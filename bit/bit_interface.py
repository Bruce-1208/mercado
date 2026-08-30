import queue
import json
import re
from collections import deque
import functools
import html
import hashlib
import hmac
import os
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
from urllib.request import urlopen

from flask import Flask, Response, request, render_template, jsonify, send_file, session, redirect, url_for, g
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
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
try:
    import bit.bit_print as bit_print
except ModuleNotFoundError as exc:
    if exc.name != "bit.bit_print":
        raise
    import bit_playwright.bit_print as bit_print
import bit.bit_reputation_info as bit_reputation_info
import bit.bit_order_sync as bit_order_sync
import bit.bit_store_link_sync as bit_store_link_sync
import bit.bit_update_orders as bit_update_orders
import bit.bit_zying_caiji as bit_zying_caiji
import bit.mercado_communications as mercado_communications
import bit.mercado_reputation as mercado_reputation
import bit.mercado_tokens as mercado_tokens
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
from datetime import datetime, timedelta, timezone
# from db_pool import get_db_connection  # 确保你的连接池文件在这个目录下


WORKBENCH_REMEMBER_HOURS = 6
WORKBENCH_SECRET_KEY_FILE = CURRENT_DIR / "runtime_locks" / "workbench_secret.key"


def resolve_workbench_secret_key(secret_file=None):
    configured = str(os.environ.get("WORKBENCH_SECRET_KEY", "")).strip()
    if configured:
        return configured

    configured_file = str(os.environ.get("WORKBENCH_SECRET_KEY_FILE", "")).strip()
    secret_path = Path(secret_file or configured_file or WORKBENCH_SECRET_KEY_FILE)
    try:
        if secret_path.exists():
            persisted = secret_path.read_text(encoding="utf-8").strip()
            if len(persisted) >= 32:
                return persisted
            logging.warning("工作台会话密钥文件内容无效，将使用本次进程临时密钥：%s", secret_path)
            return secrets.token_hex(32)

        secret_path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        try:
            file_descriptor = os.open(
                str(secret_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            persisted = secret_path.read_text(encoding="utf-8").strip()
            return persisted if len(persisted) >= 32 else secrets.token_hex(32)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as secret_handle:
            secret_handle.write(generated)
        return generated
    except OSError as exc:
        logging.warning("无法持久化工作台会话密钥，将使用本次进程临时密钥：%s", exc)
        return secrets.token_hex(32)


app = Flask(__name__, template_folder=str(resolve_template_dir()))
app.secret_key = resolve_workbench_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=WORKBENCH_REMEMBER_HOURS),
    SESSION_REFRESH_EACH_REQUEST=False,
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
    ("customer_service", "客户消息", (("customer_service.view", "查看"), ("customer_service.manage", "回复/删除"))),
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

try:
    YANDEX_CONSOLE_PORT = int(os.environ.get("WORKBENCH_YANDEX_PORT", "8011"))
except ValueError:
    YANDEX_CONSOLE_PORT = 8011
YANDEX_CONSOLE_HOST = "127.0.0.1"
YANDEX_CONSOLE_BASE_URL = f"http://{YANDEX_CONSOLE_HOST}:{YANDEX_CONSOLE_PORT}"
YANDEX_PACKAGE_ROOT = PROJECT_ROOT / "yandex"
_yandex_console_lock = threading.Lock()
_yandex_console_process = None


def _yandex_console_health():
    try:
        with urlopen(f"{YANDEX_CONSOLE_BASE_URL}/api/health", timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(
            response.status == 200
            and payload.get("status") == "ok"
            and payload.get("service") == "yandex-console"
        )
    except Exception:
        return False


def _yandex_console_python():
    configured = str(os.environ.get("WORKBENCH_YANDEX_PYTHON", "")).strip()
    if configured:
        return Path(configured)
    if os.name == "nt":
        return YANDEX_PACKAGE_ROOT / ".venv" / "Scripts" / "python.exe"
    return YANDEX_PACKAGE_ROOT / ".venv" / "bin" / "python"


def ensure_yandex_console():
    global _yandex_console_process
    if _yandex_console_health():
        return True, "Yandex 控制台已运行"

    with _yandex_console_lock:
        if _yandex_console_health():
            return True, "Yandex 控制台已运行"
        python_executable = _yandex_console_python()
        if not python_executable.exists():
            return False, "Yandex 运行环境不存在，请先执行 .\\yandex\\run.ps1 完成安装"
        if not (YANDEX_PACKAGE_ROOT / "__main__.py").exists():
            return False, "mercado/yandex 包不完整，缺少 __main__.py"

        data_dir = YANDEX_PACKAGE_ROOT / ".data"
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = data_dir / "workbench-server.log"
        environment = os.environ.copy()
        environment["YANDEX_HOST"] = YANDEX_CONSOLE_HOST
        environment["YANDEX_PORT"] = str(YANDEX_CONSOLE_PORT)
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        try:
            with log_path.open("ab", buffering=0) as log_file:
                _yandex_console_process = subprocess.Popen(
                    [str(python_executable), "-m", "yandex"],
                    cwd=str(PROJECT_ROOT),
                    env=environment,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                    start_new_session=os.name != "nt",
                )
        except Exception as exc:
            logging.exception("启动 Yandex 控制台失败")
            return False, f"启动 Yandex 控制台失败：{exc}"

        for _ in range(40):
            if _yandex_console_health():
                return True, "Yandex 控制台已启动"
            if _yandex_console_process.poll() is not None:
                break
            time.sleep(0.25)
        return False, f"Yandex 控制台启动失败，请检查日志：{log_path}"


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

    # 武汉泽顺工作台当前直接连接局域网 MySQL（192.168.1.11）。需要恢复
    # 服务端 API 时可显式设置 BIT_INTERFACE_DB_MODE=api。
    return False


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
    db_list_orders = bit_db_api.list_orders
    db_insert_task_record = bit_db_api.insert_task_record
    db_insert_zying_product_info = bit_db_api.insert_zying_product_info
    db_upsert_zying_products_to_products = bit_db_api.upsert_zying_products_to_products
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
    db_create_mercado_collection_task = bit_db_api.create_mercado_collection_task
    db_update_mercado_collection_task = bit_db_api.update_mercado_collection_task
    db_get_mercado_collection_task = bit_db_api.get_mercado_collection_task
    db_upsert_mercado_collection_items = bit_db_api.upsert_mercado_collection_items
    db_list_mercado_collection_items = bit_db_api.list_mercado_collection_items
    db_list_mercado_product_items = bit_db_api.list_mercado_product_items
    db_add_mercado_collection_items_to_products = bit_db_api.add_mercado_collection_items_to_products
    db_delete_mercado_collection_items = bit_db_api.delete_mercado_collection_items
    db_delete_mercado_product_items = bit_db_api.delete_mercado_product_items
    db_move_mercado_product_items_to_collection = bit_db_api.move_mercado_product_items_to_collection
    db_get_mercado_product_items_by_ids = bit_db_api.get_mercado_product_items_by_ids
    db_update_mercado_product_publish_state = bit_db_api.update_mercado_product_publish_state
    db_create_mercado_product_publish_records = bit_db_api.create_mercado_product_publish_records
    db_get_mercado_product_publish_records_by_ids = bit_db_api.get_mercado_product_publish_records_by_ids
    db_get_published_mercado_product_item_ids = bit_db_api.get_published_mercado_product_item_ids
    db_update_mercado_product_publish_record = bit_db_api.update_mercado_product_publish_record
    db_list_mercado_product_publish_records = bit_db_api.list_mercado_product_publish_records
    db_update_mercado_product_review_status = bit_db_api.update_mercado_product_review_status
    db_update_mercado_product_item = bit_db_api.update_mercado_product_item
    db_list_mercado_store_links = bit_db_api.list_mercado_store_links
    db_bulk_update_mercado_store_links = bit_db_api.bulk_update_mercado_store_links
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
        list_orders,
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
    db_list_orders = list_orders
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
    from erp.mercadolibre_collection_store import (
        add_collection_items_to_products as db_add_mercado_collection_items_to_products,
        create_collection_task as db_create_mercado_collection_task,
        delete_collection_items as db_delete_mercado_collection_items,
        delete_product_items as db_delete_mercado_product_items,
        get_collection_task as db_get_mercado_collection_task,
        get_product_items_by_ids as db_get_mercado_product_items_by_ids,
        move_product_items_to_collection as db_move_mercado_product_items_to_collection,
        create_product_publish_records as db_create_mercado_product_publish_records,
        get_product_publish_records_by_ids as db_get_mercado_product_publish_records_by_ids,
        get_published_product_item_ids as db_get_published_mercado_product_item_ids,
        list_product_publish_records as db_list_mercado_product_publish_records,
        list_collection_items as db_list_mercado_collection_items,
        list_product_items as db_list_mercado_product_items,
        update_collection_task as db_update_mercado_collection_task,
        update_product_publish_state as db_update_mercado_product_publish_state,
        update_product_publish_record as db_update_mercado_product_publish_record,
        update_product_item as db_update_mercado_product_item,
        update_product_review_status as db_update_mercado_product_review_status,
        upsert_zying_products_to_products as db_upsert_zying_products_to_products,
        upsert_collection_items as db_upsert_mercado_collection_items,
    )
    from erp.mercadolibre_store_link_store import (
        bulk_update_store_links as db_bulk_update_mercado_store_links,
        list_store_links as db_list_mercado_store_links,
    )


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


_WORKBENCH_USER_REQUIRED_COLUMNS = {
    "id",
    "username",
    "password_hash",
    "display_name",
    "email",
    "department",
    "role_key",
    "is_active",
    "created_at",
    "updated_at",
}


def _workbench_schema_state(cursor):
    cursor.execute(
        """
        SELECT `TABLE_NAME` AS `table_name`
        FROM `information_schema`.`TABLES`
        WHERE `TABLE_SCHEMA` = DATABASE()
          AND `TABLE_NAME` IN ('workbench_roles', 'workbench_users')
        """
    )
    table_names = {
        str((row or {}).get("table_name") or "")
        for row in (cursor.fetchall() or [])
    }
    user_columns = set()
    if "workbench_users" in table_names:
        cursor.execute(
            """
            SELECT `COLUMN_NAME` AS `column_name`
            FROM `information_schema`.`COLUMNS`
            WHERE `TABLE_SCHEMA` = DATABASE()
              AND `TABLE_NAME` = 'workbench_users'
            """
        )
        user_columns = {
            str((row or {}).get("column_name") or "")
            for row in (cursor.fetchall() or [])
        }
    return table_names, user_columns


def _workbench_default_roles_are_current(cursor):
    cursor.execute(
        """
        SELECT `role_key`, `role_name`, `description`, `permissions_json`, `is_system`
        FROM `workbench_roles`
        WHERE `role_key` IN ('super_admin', 'operator', 'viewer')
        """
    )
    current_roles = {
        str(row.get("role_key") or ""): row
        for row in (cursor.fetchall() or [])
    }
    for role in WORKBENCH_DEFAULT_ROLES:
        current = current_roles.get(role["role_key"])
        if not current:
            return False
        current_permissions = set(
            _normalize_workbench_permissions(current.get("permissions_json"))
        )
        expected_permissions = set(role["permissions"])
        if (
            str(current.get("role_name") or "") != role["role_name"]
            or str(current.get("description") or "") != role["description"]
            or current_permissions != expected_permissions
            or bool(current.get("is_system")) != bool(role["is_system"])
        ):
            return False
    return True


def _workbench_users_are_current(cursor):
    cursor.execute(
        """
        SELECT COUNT(*) AS `total`,
               SUM(CASE WHEN `role_key` IS NULL OR `role_key` = '' THEN 1 ELSE 0 END)
                   AS `missing_role_count`
        FROM `workbench_users`
        """
    )
    row = cursor.fetchone() or {}
    return int(row.get("total") or 0) > 0 and int(row.get("missing_role_count") or 0) == 0


def _rollback_workbench_connection(connection):
    try:
        connection.rollback()
    except Exception as rollback_error:
        # 连接超时后 PyMySQL 会在 rollback() 上抛出 InterfaceError(0, "")。
        # 这里只记录清理失败，不能覆盖最初且更有价值的数据库异常。
        logging.warning(
            "工作台登录表初始化失败后的回滚未执行: %s: %s",
            type(rollback_error).__name__,
            rollback_error,
        )


def ensure_workbench_user_table():
    connection_config = dict(mysql_config)
    # 登录表初始化不能无限阻塞整个控制台启动；数据库暂时被锁定时先启动服务，
    # 后续登录请求仍会返回明确的数据库错误。
    connection_config.setdefault("connect_timeout", 5)
    connection_config.setdefault("read_timeout", 8)
    connection_config.setdefault("write_timeout", 8)
    connection = pymysql.connect(**connection_config)
    try:
        with connection.cursor() as cursor:
            table_names, user_columns = _workbench_schema_state(cursor)
            roles_exist = "workbench_roles" in table_names
            users_exist = "workbench_users" in table_names
            user_schema_ready = (
                users_exist
                and _WORKBENCH_USER_REQUIRED_COLUMNS.issubset(user_columns)
            )
            if (
                roles_exist
                and user_schema_ready
                and _workbench_default_roles_are_current(cursor)
                and _workbench_users_are_current(cursor)
            ):
                return False

            if not roles_exist:
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
            if not users_exist:
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
                if users_exist and column_name not in user_columns:
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
        return True
    except Exception:
        _rollback_workbench_connection(connection)
        raise
    finally:
        try:
            connection.close()
        except Exception as close_error:
            logging.warning(
                "关闭工作台登录表初始化连接失败: %s: %s",
                type(close_error).__name__,
                close_error,
            )


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


BROWSER_EXTENSION_TOKEN_SALT = "zeshun-browser-extension-v1"


def create_browser_extension_token(user):
    """Create a signed, short-lived token for the Chrome/Edge collector."""
    payload = {
        "id": int(user.get("id") or 0),
        "username": str(user.get("username") or ""),
        "access_version": int(user.get("access_version") or 0),
    }
    return URLSafeTimedSerializer(
        app.secret_key, salt=BROWSER_EXTENSION_TOKEN_SALT
    ).dumps(payload)


def _browser_extension_user_from_token(token):
    try:
        payload = URLSafeTimedSerializer(
            app.secret_key, salt=BROWSER_EXTENSION_TOKEN_SALT
        ).loads(
            str(token or ""),
            max_age=WORKBENCH_REMEMBER_HOURS * 60 * 60,
        )
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    if payload.get("access_version") != 1:
        return payload
    if USE_DB_API:
        user = bit_db_api.get_workbench_session_user(payload.get("id"))
    else:
        row = get_workbench_user(user_id=payload.get("id"))
        user = (
            build_workbench_session_user(row)
            if row and row.get("is_active")
            else None
        )
    if not user or user.get("username") != payload.get("username"):
        return None
    return user


def browser_extension_login_required(view_func):
    @functools.wraps(view_func)
    def wrapper(*args, **kwargs):
        authorization = str(request.headers.get("Authorization") or "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        try:
            user = _browser_extension_user_from_token(token)
        except Exception:
            logging.exception("校验泽顺商品采集助手登录状态失败")
            user = None
        if not user:
            return jsonify({"status": "error", "message": "插件登录已失效，请重新登录"}), 401
        g.browser_extension_user = user
        return view_func(*args, **kwargs)

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
            if method == "POST"
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
    if path.startswith("/api/mercado-communications/"):
        action = path.rstrip("/").rsplit("/", 1)[-1].strip().lower()
        return (
            ("customer_service.manage",)
            if method == "POST" and action != "pre-sale-translate"
            else ("customer_service.view",)
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
_api_reputation_lock = threading.Lock()
_api_reputation_logs = deque(maxlen=1000)
_api_reputation_state = {
    "running": False,
    "status": "idle",
    "message": "等待全量更新",
    "started_at": "",
    "finished_at": "",
    "elapsed_seconds": 0,
    "total_stores": 0,
    "completed_stores": 0,
    "success_stores": 0,
    "failed_stores": 0,
    "total_sites": 0,
    "rows": [],
    "failures": [],
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
_zying_collection_stop_event = threading.Event()
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
    "requires_login": False,
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
_mercado_collection_lock = threading.RLock()
_mercado_collection_stop_event = threading.Event()
_mercado_collection_state = {
    "running": False,
    "task_id": None,
    "status": "idle",
    "message": "等待启动",
    "source_url": "",
    "keyword": "",
    "source_site_id": "MLM",
    "source_site_name": "墨西哥",
    "collection_scope": "all",
    "requested_count": 0,
    "worker_count": 4,
    "candidate_count": 0,
    "processed_count": 0,
    "completed_count": 0,
    "failed_count": 0,
    "elapsed_seconds": 0,
    "current_page": 0,
    "current_item_id": "",
    "started_at": "",
    "finished_at": "",
}
_mercado_playwright_setup_state = {
    "running": False,
    "status": "idle",
    "message": "采集浏览器尚未打开",
}
_mercado_profit_refresh_lock = threading.Lock()
_mercado_profit_refresh_started = False
_mercado_profit_refresh_stop_event = threading.Event()
_mercado_shipping_rate_refresh_lock = threading.RLock()
_mercado_shipping_rate_refresh_state = {
    "running": False,
    "status": "idle",
    "message": "等待从官方更新",
    "started_at": "",
    "finished_at": "",
    "elapsed_seconds": 0,
    "success_sites": 0,
    "failed_sites": 0,
    "unavailable_site_count": 0,
    "recalculation_count": 0,
    "sites": [],
    "errors": [],
    "unavailable_sites": [],
}
_mercado_publish_lock = threading.RLock()
_mercado_publish_state = {
    "running": False,
    "batch_id": "",
    "status": "idle",
    "message": "等待选择产品上架",
    "selection_mode": "accounts",
    "token_id": None,
    "token_ids": [],
    "group_names": [],
    "store_name": "",
    "site_id": "MLM",
    "site_ids": ["MLM"],
    "site_name": "墨西哥",
    "target_count": 0,
    "completed_target_count": 0,
    "skipped_target_count": 0,
    "quantity": 500,
    "worker_count": 10,
    "requested_count": 0,
    "processed_count": 0,
    "published_count": 0,
    "failed_count": 0,
    "moved_to_collection_count": 0,
    "skipped_published_count": 0,
    "elapsed_seconds": 0,
    "average_seconds_per_item": 0,
    "items_per_minute": 0,
    "estimated_remaining_seconds": 0,
    "average_stage_seconds": {},
    "current_item_id": "",
    "started_at": "",
    "finished_at": "",
    "results": [],
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
    "partial": 0,
    "no_orders": 0,
    "failed": 0,
    "skipped": 0,
    "printed_order_count": 0,
    "shipment_count": 0,
    "fallback_store_count": 0,
    "task_id": "",
    "download_path": "",
    "download_name": "",
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
    params = {
        "number": end_page,
        "window_id": window_id or bit_zying_caiji.DEFAULT_ZYING_WINDOW_ID,
        "start_page": start_page,
        "category": str(data.get("category") or "").strip()[:1024] or None,
    }
    if "category_name" in data:
        params["category_name"] = str(data.get("category_name") or "").strip()[:1024]
    # browser_type/window_name 是工作台的新参数；未提交时维持旧接口返回结构，
    # 兼容仍按 BitBrowser 窗口 ID 调用的脚本和客户端。
    if "browser_type" in data or "window_name" in data:
        params.update(
            {
                "browser_type": bit_zying_caiji.normalize_zying_browser_type(
                    data.get("browser_type")
                ),
                "window_name": str(data.get("window_name") or "").strip()[:256],
            }
        )
    return params


def build_zying_login_params(data):
    data = data if isinstance(data, dict) else {}
    window_id = str(data.get("window_id") or "").strip()[:128]
    return {
        "browser_type": bit_zying_caiji.normalize_zying_browser_type(
            data.get("browser_type")
        ),
        "window_id": window_id or bit_zying_caiji.DEFAULT_ZYING_WINDOW_ID,
        "window_name": str(data.get("window_name") or "").strip()[:256],
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
            stop_event=_zying_collection_stop_event,
            product_writer=db_insert_zying_product_info,
            existing_product_id_reader=db_get_existing_zying_product_ids,
            product_mirror_writer=db_upsert_zying_products_to_products,
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
                    "requires_login": False,
                }
            )
    except bit_zying_caiji.ZyingCollectionStopped as exc:
        stopped_message = str(exc) or "智赢产品采集已由用户结束"
        _append_zying_collection_log(stopped_message)
        with _zying_collection_state_lock:
            _zying_collection_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "stopped",
                    "message": stopped_message,
                    "summary": {},
                    "requires_login": False,
                }
            )
    except Exception as exc:
        logging.error("智赢产品采集失败：%s", exc)
        traceback.print_exc()
        _append_zying_collection_log(f"采集失败：{exc}")
        requires_login = isinstance(exc, bit_zying_caiji.ZyingAuthenticationError)
        with _zying_collection_state_lock:
            _zying_collection_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "error",
                    "message": str(exc),
                    "summary": {},
                    "requires_login": requires_login,
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
        snapshot = {
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
        snapshot.pop("download_path", None)
        snapshot["download_url"] = (
            "/api/order-print/download"
            if _order_print_state.get("download_path")
            else ""
        )
        return snapshot


def _order_print_history_status(outcome):
    text = str(outcome or "").strip()
    if "无待打印订单" in text:
        return "no_orders"
    if text.startswith("成功"):
        return "printed"
    if text.startswith("部分成功"):
        return "partial"
    if text.startswith("跳过"):
        return "skipped"
    if text.startswith("失败"):
        return "failed"
    return "unknown"


def _load_order_print_site_last_runs(current_results=None):
    """汇总全部 API 授权店铺站点及其最近一次订单打印时间。"""

    current_results = [dict(row) for row in (current_results or [])]
    configs_loaded = True
    try:
        configs = _order_print_config_options()["shops"]
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
        for site in config.get("sites") or []:
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


def _order_print_config_options():
    """Return selectable stores from Mercado API authorizations only."""

    token_rows = list((bit_db_api.list_mercado_store_tokens() or {}).get("rows") or [])
    shops = []
    site_order = []
    for token in token_rows:
        token_id = int(token.get("id") or 0)
        shop_name = str(
            token.get("display_name") or token.get("nickname") or token_id or ""
        ).strip()
        if not token_id or not shop_name:
            continue
        sites = []
        salespeople = []
        for setting in token.get("site_settings") or []:
            site_id = str(setting.get("site_id") or "").strip().upper()
            site_name = str(
                setting.get("site_name")
                or bit_print.SITE_NAMES.get(site_id)
                or ""
            ).strip()
            if site_name and site_name not in sites:
                sites.append(site_name)
            salesperson = str(setting.get("salesperson") or "").strip()
            if salesperson and salesperson not in salespeople:
                salespeople.append(salesperson)
        default_site = bit_print.SITE_NAMES.get(
            str(token.get("site_id") or "").strip().upper()
        )
        if default_site and default_site not in sites:
            sites.append(default_site)
        if not sites:
            sites = list(bit_print.SITE_IDS)
        for site in sites:
            if site not in site_order:
                site_order.append(site)
        shops.append(
            {
                "token_id": token_id,
                "shop_name": shop_name,
                "salesperson": "、".join(salespeople),
                "sites": sites,
                "token_status": str(token.get("status") or "unknown"),
                "token_status_text": str(token.get("status_text") or ""),
            }
        )
    return {"shops": shops, "sites": site_order}


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
    local_tz = datetime.now().astimezone().tzinfo
    local_now = datetime.now(local_tz)

    def parse_range_value(name, label, default):
        raw = str(data.get(name) or "").strip()
        if not raw:
            return default
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label}格式无效，请重新选择") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(local_tz)

    range_end = parse_range_value("date_to", "结束时间", local_now)
    range_start = parse_range_value(
        "date_from",
        "开始时间",
        range_end - timedelta(hours=bit_print.DEFAULT_FALLBACK_HOURS),
    )
    if range_end > local_now + timedelta(minutes=5):
        range_end = local_now
    if range_start >= range_end:
        raise ValueError("订单打印开始时间必须早于结束时间")
    if range_end - range_start > timedelta(days=31):
        raise ValueError("单次订单打印时间段不能超过 31 天")

    raw_targets = data.get("targets")
    selected_targets = []
    if raw_targets is not None:
        if not isinstance(raw_targets, list):
            raise ValueError("targets 必须是数组")
        if not raw_targets:
            raise ValueError("请至少选择一个店铺站点")
        if len(raw_targets) > 1000:
            raise ValueError("单次最多选择 1000 个店铺站点")

        options = _order_print_config_options()
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
        selected_shops = _normalized_collection_list(data, "shops")
        selected_sites = _normalized_collection_list(data, "sites")
        options = _order_print_config_options()
        configured = {shop["shop_name"]: shop for shop in options["shops"]}
        unknown_shops = [shop for shop in selected_shops if shop not in configured]
        if unknown_shops:
            raise ValueError("API 授权店铺不存在：" + "、".join(unknown_shops))
        unknown_sites = [site for site in selected_sites if site not in options["sites"]]
        if unknown_sites:
            raise ValueError("站点不存在：" + "、".join(unknown_sites))
        matching_shops = [
            shop
            for shop in selected_shops
            if any(site in selected_sites for site in configured[shop]["sites"])
        ]
        if not matching_shops:
            raise ValueError("所选 API 授权店铺没有匹配站点")
        target_label = f"{len(matching_shops)} 家 API 授权店铺 / {len(selected_sites)} 个站点"
    return {
        "task_id": secrets.token_hex(16),
        "mode": "once",
        "max_retries": _parse_int_param(
            data, "max_retries", 3, min_value=1, max_value=3
        ),
        "retry_delay_seconds": _parse_int_param(
            data, "retry_delay_seconds", 3, min_value=0, max_value=60
        ),
        "fallback_hours": bit_print.DEFAULT_FALLBACK_HOURS,
        "start_at": range_start.astimezone(timezone.utc).isoformat(),
        "end_at": range_end.astimezone(timezone.utc).isoformat(),
        "date_from": range_start.strftime("%Y-%m-%dT%H:%M"),
        "date_to": range_end.strftime("%Y-%m-%dT%H:%M"),
        "range_label": (
            f"{range_start.strftime('%Y-%m-%d %H:%M')} 至 "
            f"{range_end.strftime('%Y-%m-%d %H:%M')}"
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
            _order_print_state["message"] = "正在通过美客多 API 生成面单"
        _append_order_print_log(f"{get_now_time()} ===== 美客多 API 订单打印开始 =====")
        summary = bit_print.print_orders_all(
            selected_shops=params["selected_shops"],
            selected_sites=params["selected_sites"],
            selected_targets=params.get("selected_targets"),
            max_retries=params["max_retries"],
            retry_delay_seconds=params["retry_delay_seconds"],
            fallback_hours=params.get(
                "fallback_hours", bit_print.DEFAULT_FALLBACK_HOURS
            ),
            start_at=params.get("start_at"),
            end_at=params.get("end_at"),
            stop_event=stop_event,
            logger=_append_order_print_log,
            task_id=params.get("task_id"),
        )
        site_last_runs = _load_order_print_site_last_runs(summary.get("results", []))
        with _order_print_lock:
            _order_print_state.update(
                {
                    "printed": summary.get("printed", 0),
                    "partial": summary.get("partial", 0),
                    "no_orders": summary.get("no_orders", 0),
                    "failed": summary.get("failed", 0),
                    "skipped": summary.get("skipped", 0),
                    "printed_order_count": summary.get("printed_order_count", 0),
                    "shipment_count": summary.get("shipment_count", 0),
                    "fallback_store_count": summary.get("fallback_store_count", 0),
                    "download_path": summary.get("download_path", ""),
                    "download_name": summary.get("download_name", ""),
                    "results": summary.get("results", []),
                    "site_last_runs": site_last_runs,
                }
            )

        stopped = stop_event.is_set()
        failed_sites = int(summary.get("failed") or 0)
        partial_sites = int(summary.get("partial") or 0)
        has_output = bool(summary.get("download_path"))
        if stopped:
            final_status = "stopped"
            final_message = "API 订单打印已停止"
        elif failed_sites and not has_output:
            final_status = "error"
            final_message = f"没有生成面单，{failed_sites} 个站点执行失败"
        elif failed_sites or partial_sites:
            final_status = "partial"
            final_message = (
                f"已生成 {summary.get('shipment_count', 0)} 个面单；"
                f"{partial_sites} 个站点部分成功，{failed_sites} 个站点失败"
            )
        else:
            final_status = "success"
            final_message = (
                f"API 面单已生成：{summary.get('shipment_count', 0)} 个"
                if has_output
                else "没有需要打印的订单"
            )
        with _order_print_lock:
            _order_print_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": final_status,
                    "message": final_message,
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
    raw_salespeople = data.get("salespeople", data.get("salesperson", []))
    if isinstance(raw_salespeople, str):
        raw_salespeople = [raw_salespeople]
    salespeople = []
    for value in raw_salespeople or ():
        salesperson = str(value or "").strip()
        if salesperson in ("", "全部业务员", "所有业务员", "all", "*"):
            continue
        if salesperson not in salespeople:
            salespeople.append(salesperson)
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
        "salespeople": salespeople,
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
                min_rate=min_rate,
                salespeople=params["salespeople"],
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
                min_rate=min_rate,
                salespeople=params["salespeople"],
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


@app.route('/api/order-print/options', methods=['GET'])
@login_required
def api_order_print_options():
    try:
        response = jsonify({"status": "success", "data": _order_print_config_options()})
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        logging.error("读取 API 打印店铺失败：%s", exc)
        return jsonify({
            "status": "error",
            "message": f"读取美客多授权店铺失败：{exc}",
        }), 500


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
            "message": f"{params['target']} API 订单打印已启动",
            "params": params,
            "task_id": params["task_id"],
            "printed": 0,
            "partial": 0,
            "no_orders": 0,
            "failed": 0,
            "skipped": 0,
            "printed_order_count": 0,
            "shipment_count": 0,
            "fallback_store_count": 0,
            "download_path": "",
            "download_name": "",
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
        "message": "美客多 API 订单打印已在后台启动",
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
            "message": "正在停止，当前 API 请求完成后会安全结束",
        })
        state = _order_print_snapshot()
    _append_order_print_log(f"{get_now_time()} 已收到停止 API 订单打印请求")
    return jsonify({
        "status": "success",
        "data": state,
        "message": "已发送停止指令",
    })


@app.route('/api/order-print/status', methods=['GET'])
@login_required
def api_order_print_status():
    state = _order_print_snapshot()
    # Polling happens every two seconds while a task runs.  Re-querying token
    # and history tables on every poll can make the status endpoint itself
    # unavailable when the database or Mercado sync is busy.
    if not state.get("site_last_runs"):
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


@app.route('/api/order-print/download', methods=['GET'])
@login_required
def api_order_print_download():
    with _order_print_lock:
        raw_path = str(_order_print_state.get("download_path") or "")
        download_name = str(_order_print_state.get("download_name") or "")
    if not raw_path:
        return jsonify({"status": "error", "message": "当前没有可下载的 API 面单"}), 404
    path = Path(raw_path).resolve()
    output_root = bit_print.ORDER_PRINT_OUTPUT_DIR.resolve()
    if not path.is_relative_to(output_root) or not path.is_file():
        return jsonify({"status": "error", "message": "面单文件不存在或已失效"}), 404
    response = send_file(
        path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=download_name or path.name,
        max_age=0,
    )
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


def _order_list_query_params(args):
    salespeople = []
    for value in args.getlist("salesperson"):
        name = str(value or "").strip()
        if name and name not in salespeople:
            salespeople.append(name)
    store_ids = []
    for value in args.getlist("store_id"):
        text = str(value or "").strip()
        if not text:
            continue
        try:
            store_id = int(text)
        except ValueError as exc:
            raise ValueError("店铺筛选参数无效") from exc
        if store_id <= 0:
            raise ValueError("店铺筛选参数无效")
        if store_id not in store_ids:
            store_ids.append(store_id)
    params = {
        "country": str(args.get("country") or "").strip(),
        "status": str(args.get("status") or "").strip(),
        "salesperson": salespeople[0] if len(salespeople) == 1 else "",
        "group_name": str(args.get("group_name") or "").strip(),
        "search": str(args.get("search") or "").strip(),
        "start_date": str(args.get("start_date") or "").strip(),
        "end_date": str(args.get("end_date") or "").strip(),
        "origin": str(args.get("origin") or "").strip(),
        "page": _parse_int_param(args, "page", 1, 1, 1000000),
        "page_size": _parse_int_param(args, "page_size", 200, 10, 200),
    }
    if len(salespeople) > 1:
        params["salespeople"] = salespeople
    if store_ids:
        params["store_ids"] = store_ids
    return params


@app.route('/api/orders', methods=['GET'])
@login_required
def api_orders():
    try:
        data = db_list_orders(**_order_list_query_params(request.args))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("订单列表加载失败")
        return jsonify({
            "status": "error",
            "message": f"订单列表加载失败：{exc}",
        }), 502
    response = jsonify({"status": "success", "data": data})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/order-sync/options', methods=['GET'])
@login_required
def api_order_sync_options():
    try:
        data = bit_db_api.list_mercado_store_tokens() or {}
        return jsonify({"status": "success", "data": data})
    except Exception as exc:
        logging.exception("读取订单同步店铺失败")
        return jsonify({"status": "error", "message": f"读取授权店铺失败：{exc}"}), 502


@app.route('/api/orders/bulk-update', methods=['POST'])
@login_required
def api_bulk_update_orders():
    data = request.get_json(silent=True) or {}
    order_ids = data.get("order_ids") or []
    if not isinstance(order_ids, list):
        return jsonify({"status": "error", "message": "order_ids 必须是数组"}), 422
    changes = {}
    for field in (
        "workflow_status", "purchase_order", "purchase_tracking",
        "logistics_company", "purchase_cost", "purchase_remark",
    ):
        if field in data:
            changes[field] = data.get(field)
    user = session.get("workbench_user") or {}
    try:
        result = bit_db_api.bulk_update_orders(
            order_ids,
            operator_id=user.get("id"),
            operator_name=user.get("display_name") or user.get("username") or "",
            **changes,
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("批量更新订单失败")
        return jsonify({"status": "error", "message": f"批量更新订单失败：{exc}"}), 502
    return jsonify({
        "status": "success",
        "message": f"已更新 {int(result.get('matched') or 0)} 个订单",
        "data": result,
    })


@app.route('/api/orders/print', methods=['POST'])
@login_required
def api_print_orders_pdf():
    data = request.get_json(silent=True) or {}
    order_ids = data.get("order_ids") or []
    if not isinstance(order_ids, list):
        return jsonify({"status": "error", "message": "order_ids 必须是数组"}), 422
    try:
        result = bit_db_api.download_order_labels(order_ids)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("美客多面单 PDF 下载失败")
        return jsonify({"status": "error", "message": f"美客多面单下载失败：{exc}"}), 502

    print_log_error = ""
    try:
        user = session.get("workbench_user") or {}
        bit_db_api.record_order_print_logs(
            result.get("order_ids") or [],
            operator_id=user.get("id"),
            operator_name=user.get("display_name") or user.get("username") or "",
        )
    except Exception as exc:
        # The PDF is already valid at this point.  Do not discard it just
        # because the audit write failed; report the condition to the UI so
        # operators can avoid immediately printing the same orders again.
        print_log_error = str(exc) or exc.__class__.__name__
        logging.exception("美客多面单已生成，但打印记录写入失败")
    response = send_file(
        BytesIO(result["content"]),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=result.get("filename") or "mercado-labels.pdf",
        max_age=0,
    )
    successful_order_ids = [str(value) for value in result.get("order_ids") or []]
    skipped_order_count = int(
        result.get("skipped_order_count")
        or len(result.get("skipped_order_ids") or [])
    )
    failed_order_count = int(
        result.get("failed_order_count")
        or len(result.get("failed_order_ids") or [])
    )
    response.headers["X-Mercado-Shipment-Count"] = str(result.get("shipment_count") or 0)
    response.headers["X-Mercado-Printed-Order-Count"] = str(len(successful_order_ids))
    response.headers["X-Mercado-Printed-Order-Ids"] = ",".join(successful_order_ids)
    response.headers["X-Mercado-Skipped-Order-Count"] = str(skipped_order_count)
    response.headers["X-Mercado-Failed-Order-Count"] = str(failed_order_count)
    response.headers["X-Mercado-Result"] = (
        "partial" if skipped_order_count or failed_order_count else "success"
    )
    if print_log_error:
        response.headers["X-Mercado-Print-Log"] = "failed"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/orders/<order_id>/logs', methods=['GET'])
@login_required
def api_order_operation_logs(order_id):
    try:
        rows = bit_db_api.list_order_operation_logs(
            order_id,
            limit=_parse_int_param(request.args, "limit", 100, 1, 200),
        )
    except Exception as exc:
        logging.exception("订单操作日志加载失败")
        return jsonify({"status": "error", "message": f"订单操作日志加载失败：{exc}"}), 502
    response = jsonify({"status": "success", "data": {"rows": rows}})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/orders/<order_id>/tracking', methods=['GET'])
@login_required
def api_order_tracking(order_id):
    try:
        data = bit_db_api.get_order_tracking(order_id)
    except (KeyError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        logging.exception("物流轨迹查询失败")
        return jsonify({"status": "error", "message": f"物流轨迹查询失败：{exc}"}), 502
    response = jsonify({"status": "success", "data": data})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/order-sync/start', methods=['POST'])
@login_required
def api_start_order_sync():
    data = request.get_json(silent=True) or {}
    token_ids = data.get("token_ids") or []
    if not isinstance(token_ids, list):
        return jsonify({"status": "error", "message": "token_ids 必须是数组"}), 422
    try:
        result = bit_db_api.start_order_sync(
            start_date=str(data.get("start_date") or "").strip(),
            end_date=str(data.get("end_date") or "").strip(),
            token_ids=token_ids,
            mode="manual",
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("启动订单同步失败")
        return jsonify({"status": "error", "message": f"启动订单同步失败：{exc}"}), 502
    started = bool(result.get("started"))
    return jsonify({
        "status": "success" if started else "running",
        "message": "订单拉取任务已启动" if started else "已有订单同步任务正在运行",
        "data": result.get("state") or {},
    }), 202 if started else 409


@app.route('/api/order-sync/status', methods=['GET'])
@login_required
def api_order_sync_status():
    try:
        data = bit_db_api.get_order_sync_status() or {}
        response = jsonify({"status": "success", "data": data})
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        logging.exception("读取订单同步状态失败")
        return jsonify({"status": "error", "message": f"读取订单同步状态失败：{exc}"}), 502


@app.route('/api/store-links', methods=['GET'])
@login_required
def api_store_links():
    try:
        token_text = str(request.args.get("token_id") or "").strip()
        data = bit_db_api.list_mercado_store_links(
            search=str(request.args.get("search") or "").strip(),
            token_id=int(token_text) if token_text else None,
            site_id=str(request.args.get("site_id") or "").strip(),
            status=str(request.args.get("status") or "").strip(),
            sales_sort=str(request.args.get("sales_sort") or "desc").strip(),
            current_only=str(request.args.get("current_only") or "1").strip().lower()
            not in ("0", "false", "no", "off"),
            page=_parse_int_param(request.args, "page", 1, 1, 1000000),
            page_size=_parse_int_param(request.args, "page_size", 1000, 10, 1000),
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("店铺链接列表加载失败")
        return jsonify({"status": "error", "message": f"店铺链接列表加载失败：{exc}"}), 502
    response = jsonify({"status": "success", "data": data})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/store-links/bulk-update', methods=['POST'])
@login_required
def api_bulk_update_store_links():
    data = request.get_json(silent=True) or {}
    link_ids = data.get("link_ids") or []
    if not isinstance(link_ids, list):
        return jsonify({"status": "error", "message": "link_ids 必须是数组"}), 422
    allowed = (
        "price", "weight_g", "package_length_cm", "package_width_cm",
        "package_height_cm", "net_proceeds_usd",
    )
    changes = {field: data.get(field) for field in allowed if field in data}
    try:
        result = bit_db_api.bulk_update_mercado_store_links(link_ids, **changes)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("批量更新店铺链接失败")
        return jsonify({"status": "error", "message": f"批量更新店铺链接失败：{exc}"}), 502
    return jsonify({
        "status": "success",
        "message": f"已更新 {int(result.get('matched') or 0)} 条店铺链接",
        "data": result,
    })


@app.route('/api/store-links/sync/start', methods=['POST'])
@login_required
def api_start_store_link_sync():
    data = request.get_json(silent=True) or {}
    sync_all = data.get("sync_all") is True
    token_ids = [] if sync_all else (data.get("token_ids") or [])
    if not isinstance(token_ids, list):
        return jsonify({"status": "error", "message": "token_ids 必须是数组"}), 422
    try:
        result = bit_db_api.start_store_link_sync(token_ids)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("启动店铺链接同步失败")
        return jsonify({"status": "error", "message": f"启动店铺链接同步失败：{exc}"}), 502
    started = bool(result.get("started"))
    return jsonify({
        "status": "success" if started else "running",
        "message": "店铺链接同步已启动" if started else "已有店铺链接同步任务正在运行",
        "data": result.get("state") or {},
    }), 202 if started else 409


@app.route('/api/store-links/sync/status', methods=['GET'])
@login_required
def api_store_link_sync_status():
    try:
        data = bit_db_api.get_store_link_sync_status() or {}
        response = jsonify({"status": "success", "data": data})
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        logging.exception("读取店铺链接同步状态失败")
        return jsonify({"status": "error", "message": f"读取店铺链接同步状态失败：{exc}"}), 502


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


@app.route('/api/tasks/daily/options', methods=['GET'])
@login_required
def api_daily_task_options():
    salespeople = []
    try:
        users = _workbench_backend("list_workbench_users") or []
        salespeople.extend(
            str(user.get("display_name") or user.get("username") or "").strip()
            for user in users
            if user.get("is_active") is not False
        )
    except Exception:
        logging.exception("读取任务模块业务员列表失败，回退到店铺授权配置")
    try:
        token_data = bit_db_api.list_mercado_store_tokens() or {}
        for token in token_data.get("rows") or ():
            for setting in token.get("site_settings") or ():
                salespeople.append(str(setting.get("salesperson") or "").strip())
    except Exception:
        logging.exception("从店铺授权读取任务模块业务员失败")
    unique_salespeople = sorted(
        {name for name in salespeople if name},
        key=lambda value: value.casefold(),
    )
    return jsonify({
        "status": "success",
        "data": {"salespeople": unique_salespeople},
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


@app.route('/api/zying-collection/auth/open', methods=['POST'])
@login_required
def api_open_zying_collection_login():
    try:
        params = build_zying_login_params(request.get_json(silent=True) or {})
        result = bit_zying_caiji.open_zying_login_window(**params)
        return jsonify({
            "status": "success",
            "message": result.get("message") or "智赢登录窗口已打开",
            "data": {
                **result,
                "auth": bit_zying_caiji.get_zying_auth_status(),
            },
        })
    except Exception as exc:
        logging.error("打开智赢登录窗口失败：%s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/zying-collection/auth/capture', methods=['POST'])
@login_required
def api_capture_zying_collection_login():
    try:
        params = build_zying_login_params(request.get_json(silent=True) or {})
        auth = bit_zying_caiji.capture_zying_login_from_browser(**params)
        with _zying_collection_state_lock:
            _zying_collection_state["requires_login"] = False
            if not _zying_collection_state.get("running"):
                _zying_collection_state["message"] = "智赢登录状态已保存，可以启动后台采集"
        return jsonify({
            "status": "success",
            "message": "智赢登录状态有效，凭证已保存",
            "data": {"auth": auth},
        })
    except bit_zying_caiji.ZyingAuthenticationError as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
            "requires_login": True,
        }), 409
    except Exception as exc:
        logging.error("保存智赢登录状态失败：%s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 400


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
        _zying_collection_stop_event.clear()
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
                "requires_login": False,
            }
        )
        _append_zying_collection_log(
            f"智赢采集任务已启动：第 {params['start_page']}-{params['number']} 页，"
            f"分类 {params.get('category_name') or params.get('category') or '全部'}，"
            "模式 后台 API；"
            "数据库已有产品将直接跳过"
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
            "auth": bit_zying_caiji.get_zying_auth_status(),
            "defaults": {
                "window_id": bit_zying_caiji.DEFAULT_ZYING_WINDOW_ID,
                "browser_type": bit_zying_caiji.normalize_zying_browser_type(
                    bit_zying_caiji.DEFAULT_ZYING_BROWSER_TYPE
                ),
                "window_name": "",
                "start_page": bit_zying_caiji.DEFAULT_ZYING_START_PAGE,
                "end_page": bit_zying_caiji.DEFAULT_ZYING_PAGE_COUNT,
            },
        }
    return jsonify({"status": "success", "data": data})


@app.route('/api/zying-collection/stop', methods=['POST'])
@login_required
def api_stop_zying_collection():
    with _zying_collection_state_lock:
        if not _zying_collection_state.get("running"):
            return jsonify({
                "status": "error",
                "message": "当前没有正在运行的智赢产品采集任务",
                "data": {
                    **dict(_zying_collection_state),
                    "logs": list(_zying_collection_logs),
                },
            }), 409
        _zying_collection_stop_event.set()
        _zying_collection_state.update(
            {
                "status": "stopping",
                "message": "正在安全结束智赢产品采集，请等待当前接口或入库节点完成",
            }
        )
        _append_zying_collection_log("已收到结束指令，正在安全停止采集")
        data = {
            **dict(_zying_collection_state),
            "logs": list(_zying_collection_logs),
        }
    return jsonify({
        "status": "success",
        "message": "已发送结束指令",
        "data": data,
    })


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


def _mercado_collection_state_update(**changes):
    with _mercado_collection_lock:
        _mercado_collection_state.update(changes)


def _mercado_collection_finish_status(processed, completed, failed, requested=None):
    processed = max(0, int(processed or 0))
    completed = max(0, int(completed or 0))
    failed = max(0, int(failed or 0))
    requested = max(0, int(requested or 0))
    shortfall = max(0, requested - processed) if requested else 0
    if not processed or (failed >= processed and completed == 0):
        status = "error"
        prefix = "采集失败"
    elif failed or shortfall:
        status = "partial"
        prefix = "采集部分完成"
    else:
        status = "completed"
        prefix = "采集完成"
    message = (
        f"{prefix}：入库 {processed} 件，"
        f"重量尺寸完整 {completed} 件，待补充 {failed} 件"
    )
    if shortfall:
        message += f"，距离目标还差 {shortfall} 件"
    return status, message


def _mercado_collection_elapsed_seconds(state, task=None, now=None):
    """Return a stable live/final task duration for the status API."""
    state = state or {}
    task = task or {}
    started_value = state.get("started_at") or task.get("started_at")
    if not started_value:
        return max(0, int(state.get("elapsed_seconds") or task.get("elapsed_seconds") or 0))
    finished_value = state.get("finished_at") or task.get("finished_at")

    def parse(value):
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    started_at = parse(started_value)
    finished_at = parse(finished_value) or now or datetime.now()
    if started_at is None:
        return max(0, int(state.get("elapsed_seconds") or task.get("elapsed_seconds") or 0))
    return max(0, int((finished_at - started_at).total_seconds()))


def _format_mercado_elapsed(seconds):
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


def _mercado_shipping_rate_refresh_snapshot():
    with _mercado_shipping_rate_refresh_lock:
        return {
            **_mercado_shipping_rate_refresh_state,
            "sites": [dict(row) for row in _mercado_shipping_rate_refresh_state.get("sites", [])],
            "errors": [dict(row) for row in _mercado_shipping_rate_refresh_state.get("errors", [])],
            "unavailable_sites": [
                dict(row)
                for row in _mercado_shipping_rate_refresh_state.get("unavailable_sites", [])
            ],
        }


def _run_mercado_shipping_rate_refresh():
    started_monotonic = time.monotonic()
    started_at = datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    with _mercado_shipping_rate_refresh_lock:
        _mercado_shipping_rate_refresh_state.update({
            "running": True,
            "status": "running",
            "message": "正在读取 Global Selling 官方最新运费公告与汇率",
            "started_at": started_at,
            "finished_at": "",
            "elapsed_seconds": 0,
            "success_sites": 0,
            "failed_sites": 0,
            "unavailable_site_count": 0,
            "recalculation_count": 0,
            "sites": [],
            "errors": [],
            "unavailable_sites": [],
        })
    try:
        from erp.mercadolibre_collection_store import mark_all_profitability_stale
        from erp.mercadolibre_profitability import (
            MercadoProfitabilityClient,
            active_store_token,
        )
        from erp.mercadolibre_shipping_rate_cards import (
            refresh_official_shipping_rate_cards,
        )

        # The announcements are public. Mercado Libre currently requires an
        # authorized app token for its official currency-conversion endpoint.
        result = refresh_official_shipping_rate_cards(
            MercadoProfitabilityClient(active_store_token())
        )
        recalculation_count = mark_all_profitability_stale()
        success_sites = int(result.get("success_sites") or 0)
        failed_sites = int(result.get("failed_sites") or 0)
        unavailable_site_count = int(result.get("unavailable_site_count") or 0)
        status = "completed" if success_sites and not failed_sites else (
            "partial" if success_sites else "failed"
        )
        message = (
            f"官方标准更新完成：已更新 {success_sites} 个站点，"
            f"官方未公布 {unavailable_site_count} 个，失败 {failed_sites} 个；"
            f"已安排 {recalculation_count} 条商品成本重算"
        )
        with _mercado_shipping_rate_refresh_lock:
            _mercado_shipping_rate_refresh_state.update({
                "running": False,
                "status": status,
                "message": message,
                "finished_at": datetime.now().replace(microsecond=0).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "elapsed_seconds": round(time.monotonic() - started_monotonic, 1),
                "success_sites": success_sites,
                "failed_sites": failed_sites,
                "unavailable_site_count": unavailable_site_count,
                "recalculation_count": recalculation_count,
                "sites": list(result.get("sites") or []),
                "errors": list(result.get("errors") or []),
                "unavailable_sites": list(result.get("unavailable_sites") or []),
            })
    except Exception as exc:
        logging.exception("从官方刷新 Mercado 运费表失败")
        with _mercado_shipping_rate_refresh_lock:
            _mercado_shipping_rate_refresh_state.update({
                "running": False,
                "status": "failed",
                "message": f"Global Selling 官方标准更新失败：{exc}",
                "finished_at": datetime.now().replace(microsecond=0).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "elapsed_seconds": round(time.monotonic() - started_monotonic, 1),
                "failed_sites": 5,
                "errors": [{"site_id": "", "country_name": "全部站点", "error": str(exc)}],
            })


def _start_mercado_shipping_rate_refresh(*, automatic=False):
    with _mercado_shipping_rate_refresh_lock:
        if _mercado_shipping_rate_refresh_state.get("running"):
            return False
        _mercado_shipping_rate_refresh_state.update({
            "running": True,
            "status": "queued",
            "message": "官方标准已加入后台更新队列" if not automatic else "每日官方标准更新已加入队列",
        })
    threading.Thread(
        target=_run_mercado_shipping_rate_refresh,
        name="mercado-official-shipping-rate-refresh",
        daemon=True,
    ).start()
    return True


def _mercado_profit_refresh_loop():
    """Refresh stale official fee snapshots without blocking the workbench UI."""

    from erp.ecb_exchange_rates import refresh_usd_cny_daily_rates
    from erp.mercadolibre_collection_store import (
        backfill_item_exchange_prices,
        list_stale_profitability_items,
        update_item_profitability,
    )
    from erp.mercadolibre_profitability import (
        MercadoProfitabilityClient,
        SUPPORTED_SITE_CURRENCIES,
        active_store_token,
        enrich_profitability,
        refresh_supported_exchange_rates,
    )

    stale_hours = 24
    try:
        batch_size = max(1, min(int(os.environ.get("MERCADO_PROFIT_REFRESH_BATCH", "50")), 500))
    except ValueError:
        batch_size = 50
    try:
        interval = max(60, int(os.environ.get("MERCADO_PROFIT_REFRESH_SECONDS", "300")))
    except ValueError:
        interval = 300

    next_reference_check = 0.0
    while not _mercado_profit_refresh_stop_event.is_set():
        processed_rows = False
        try:
            stale_before = (datetime.now() - timedelta(hours=stale_hours)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = list_stale_profitability_items(
                stale_before=stale_before,
                limit=batch_size,
            )
            client = None
            if time.monotonic() >= next_reference_check:
                client = MercadoProfitabilityClient(active_store_token())
                site_rates = refresh_supported_exchange_rates(client)
                backfill_item_exchange_prices({
                    SUPPORTED_SITE_CURRENCIES[site_id]: snapshot
                    for site_id, snapshot in site_rates.items()
                })
                refresh_usd_cny_daily_rates()
                from erp.mercadolibre_shipping_rate_cards import (
                    OfficialShippingRateCardStore,
                )
                if OfficialShippingRateCardStore().needs_refresh(max_age_hours=24):
                    _start_mercado_shipping_rate_refresh(automatic=True)
                # Refresh reference data and backfill converted prices once a day.
                next_reference_check = time.monotonic() + 24 * 60 * 60
            if rows:
                processed_rows = True
                from concurrent.futures import ThreadPoolExecutor, as_completed

                token = active_store_token()
                worker_state = threading.local()

                def refresh_row(row):
                    row_client = getattr(worker_state, "client", None)
                    if row_client is None:
                        row_client = MercadoProfitabilityClient(token)
                        worker_state.client = row_client
                    enriched = enrich_profitability(row, client=row_client)
                    update_item_profitability(
                        str(row.get("source_item_id") or ""), enriched
                    )
                    return str(row.get("source_item_id") or "")

                worker_count = min(10, len(rows))
                with ThreadPoolExecutor(
                    max_workers=worker_count,
                    thread_name_prefix="mercado-profit",
                ) as executor:
                    futures = [executor.submit(refresh_row, row) for row in rows]
                    for future in as_completed(futures):
                        if _mercado_profit_refresh_stop_event.is_set():
                            break
                        try:
                            future.result()
                        except Exception:
                            logging.exception("Mercado 商品成本重算失败")
        except Exception:
            logging.exception("自动更新 Mercado 官网佣金、汇率和运费失败")
        # Drain a recalculation backlog continuously in 50-row/10-thread
        # batches. The long interval is only for the idle daily poll.
        _mercado_profit_refresh_stop_event.wait(0.5 if processed_rows else interval)


def ensure_mercado_profit_refresh_worker():
    global _mercado_profit_refresh_started
    if app.testing or _truthy_env(os.environ.get("MERCADO_PROFIT_REFRESH_DISABLED")):
        return
    with _mercado_profit_refresh_lock:
        if _mercado_profit_refresh_started:
            return
        thread = threading.Thread(
            target=_mercado_profit_refresh_loop,
            name="mercado-profit-refresh",
            daemon=True,
        )
        thread.start()
        _mercado_profit_refresh_started = True


@app.before_request
def _start_mercado_profit_refresh_worker():
    ensure_mercado_profit_refresh_worker()


@app.before_request
def _start_order_sync_scheduler():
    if not USE_DB_API and not app.testing:
        bit_order_sync.ensure_order_sync_scheduler()


def _mercado_collection_rows_needing_repair(rows):
    """Retry only rows whose current weight/dimensions are actually incomplete."""
    from erp.mercadolibre_collection_store import has_complete_weight_dimensions

    return [row for row in rows or [] if not has_complete_weight_dimensions(row)]


def _mercado_collection_db_call(operation, *args, attempts=6, **kwargs):
    """Retry transient database/network failures without killing browser workers."""
    attempts = max(1, min(int(attempts), 10))
    for attempt in range(attempts):
        try:
            return operation(*args, **kwargs)
        except Exception:
            if attempt + 1 >= attempts:
                raise
            delay = min(0.5 * (2 ** attempt), 8.0)
            logging.warning(
                "Mercado 采集数据库操作失败，%.1f 秒后重试（%s/%s）",
                delay,
                attempt + 1,
                attempts,
                exc_info=True,
            )
            time.sleep(delay)


def _run_mercado_collection_task(
    task_id, source_url, requested_count, worker_count, collection_scope
):
    from erp.mercadolibre_batch_collector import (
        CollectionStopped,
        DEFAULT_ZYING_WINDOW_ID,
        collect_marketplace_listing,
    )
    counters = {"candidate": 0, "processed": 0, "completed": 0, "failed": 0}
    counters_lock = threading.Lock()
    item_statuses = {}
    pending_rows = []
    pending_rows_lock = threading.Lock()
    collection_write_batch_size = 25
    started_monotonic = time.monotonic()
    last_task_status_write = 0.0

    def elapsed_seconds():
        return max(0, int(time.monotonic() - started_monotonic))

    def result_summary(message):
        return (
            f"{message} · 成功 {counters['completed']} 件 · "
            f"失败 {counters['failed']} 件 · 并发 {worker_count} · "
            f"耗时 {_format_mercado_elapsed(elapsed_seconds())}"
        )

    def on_page(info):
        counters["candidate"] = int(info.get("candidate_count") or 0)
        current_page = int(info.get("page") or 0)
        stage = str(info.get("stage") or "")
        message = str(info.get("message") or "") or (
            f"已扫描第 {current_page} 页，找到 {counters['candidate']} 个不重复商品"
        )
        state_status = "waiting_verification" if stage == "waiting_verification" else "running"
        _mercado_collection_state_update(
            status=state_status,
            candidate_count=counters["candidate"],
            current_page=current_page,
            message=message,
        )
        _mercado_collection_db_call(
            db_update_mercado_collection_task,
            task_id,
            status="running",
            collected_count=counters["candidate"],
            current_page=current_page,
            message=message,
        )

    def on_progress(info):
        _mercado_collection_state_update(
            current_item_id=str(info.get("item_id") or ""),
            message=str(info.get("message") or "正在采集商品详情"),
        )

    def buffer_collection_row(row, *, force=False):
        batch = []
        with pending_rows_lock:
            if row is not None:
                pending_rows.append(dict(row))
            if force or len(pending_rows) >= collection_write_batch_size:
                batch.extend(pending_rows)
                pending_rows.clear()
        if batch:
            _mercado_collection_db_call(
                db_upsert_mercado_collection_items, task_id, batch
            )

    def on_item(row):
        nonlocal last_task_status_write
        # Persist collection results immediately.  Official commission,
        # exchange-rate and shipping estimates are filled by the existing
        # background profitability worker and must not block browser slots.
        row = {
            **row,
            "profitability_updated_at": None,
            "profitability_source": "mercadolibre_official_api_pending",
            "profitability_error": "",
        }
        buffer_collection_row(row)
        with counters_lock:
            item_id = str(row.get("source_item_id") or "")
            incoming_status = "ok" if row.get("scrape_status") == "ok" else "partial"
            previous_status = item_statuses.get(item_id)
            effective_status = (
                "ok" if previous_status == "ok" or incoming_status == "ok" else "partial"
            )
            if previous_status is None:
                counters["processed"] += 1
                counters["completed" if effective_status == "ok" else "failed"] += 1
            elif previous_status != effective_status:
                counters["completed" if previous_status == "ok" else "failed"] -= 1
                counters["completed" if effective_status == "ok" else "failed"] += 1
            item_statuses[item_id] = effective_status
            message = (
                f"已完成 {counters['processed']}/{counters['candidate']}，"
                f"完整 {counters['completed']}，待补充 {counters['failed']}；"
                "佣金和运费在后台计算"
            )
            _mercado_collection_state_update(
                processed_count=counters["processed"],
                completed_count=counters["completed"],
                failed_count=counters["failed"],
                message=message,
            )
            now_monotonic = time.monotonic()
            persist_task_status = (
                now_monotonic - last_task_status_write >= 1.0
                or counters["processed"] >= max(counters["candidate"], requested_count)
            )
            if persist_task_status:
                last_task_status_write = now_monotonic
                task_snapshot = {
                    "collected_count": max(
                        counters["candidate"], counters["processed"]
                    ),
                    "completed_count": counters["completed"],
                    "failed_count": counters["failed"],
                    "message": message,
                }
            else:
                task_snapshot = None
        if task_snapshot is not None:
            _mercado_collection_db_call(
                db_update_mercado_collection_task,
                task_id,
                **task_snapshot,
            )

    try:
        _mercado_collection_db_call(
            db_update_mercado_collection_task,
            task_id,
            status="running",
            message="正在打开采集浏览器",
            worker_count=worker_count,
            started=True,
        )
        result = collect_marketplace_listing(
            source_url,
            requested_count,
            max_workers=worker_count,
            collection_scope=collection_scope,
            on_page=on_page,
            on_item=on_item,
            on_progress=on_progress,
            stop_event=_mercado_collection_stop_event,
        )
        buffer_collection_row(None, force=True)
        incomplete_rows = _mercado_collection_rows_needing_repair(result.get("rows"))
        if incomplete_rows and not _mercado_collection_stop_event.is_set():
            from erp.mercadolibre_playwright_collector import (
                repair_marketplace_items_playwright,
            )

            _mercado_collection_state_update(
                message=f"开始低并发补采 {len(incomplete_rows)} 件缺少重量尺寸的商品"
            )
            repair_marketplace_items_playwright(
                incomplete_rows,
                window_id=DEFAULT_ZYING_WINDOW_ID,
                plugin_timeout=15.0,
                attempts=1,
                on_item=on_item,
                on_progress=on_progress,
                stop_event=_mercado_collection_stop_event,
            )
            buffer_collection_row(None, force=True)
        status, message = _mercado_collection_finish_status(
            counters["processed"],
            counters["completed"],
            counters["failed"],
            requested_count,
        )
        message = result_summary(message)
        _mercado_collection_db_call(
            db_update_mercado_collection_task,
            task_id,
            status=status,
            message=message,
            collected_count=int(result.get("candidate_count") or counters["candidate"]),
            completed_count=counters["completed"],
            failed_count=counters["failed"],
            worker_count=worker_count,
            elapsed_seconds=elapsed_seconds(),
            finished=True,
        )
        _mercado_collection_state_update(
            status=status,
            message=message,
            elapsed_seconds=elapsed_seconds(),
        )
    except CollectionStopped:
        message = result_summary(
            f"采集已停止，已保留入库的 {counters['processed']} 件商品"
        )
        try:
            _mercado_collection_db_call(
                db_update_mercado_collection_task,
                task_id,
                status="stopped",
                message=message,
                completed_count=counters["completed"],
                failed_count=counters["failed"],
                worker_count=worker_count,
                elapsed_seconds=elapsed_seconds(),
                finished=True,
            )
        except Exception:
            logging.exception("更新 Mercado 采集停止状态失败")
        _mercado_collection_state_update(
            status="stopped", message=message, elapsed_seconds=elapsed_seconds()
        )
    except Exception as exc:
        message = result_summary(f"采集失败：{exc}")
        logging.exception("Mercado 列表采集失败")
        try:
            _mercado_collection_db_call(
                db_update_mercado_collection_task,
                task_id,
                status="error",
                message=message,
                completed_count=counters["completed"],
                failed_count=counters["failed"],
                worker_count=worker_count,
                elapsed_seconds=elapsed_seconds(),
                finished=True,
            )
        except Exception:
            logging.exception("更新 Mercado 采集失败状态失败")
        _mercado_collection_state_update(
            status="error", message=message, elapsed_seconds=elapsed_seconds()
        )
    finally:
        try:
            buffer_collection_row(None, force=True)
        except Exception:
            logging.exception("刷新 Mercado 采集批量写入缓冲区失败")
        _mercado_collection_state_update(
            running=False,
            current_item_id="",
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )


@app.route('/api/mercado-collection/start', methods=['POST'])
@login_required
def api_start_mercado_collection():
    from erp.mercadolibre_batch_collector import (
        DEFAULT_COLLECTION_WORKERS,
        build_marketplace_search_url,
        normalize_collection_scope,
        normalize_collection_workers,
        validate_collection_request,
    )
    from erp.mercadolibre_translation import (
        marketplace_site_name,
        normalize_marketplace_site,
    )

    data = request.get_json(silent=True) or {}
    try:
        raw_source_url = str(data.get("source_url") or "").strip()
        keyword = str(data.get("keyword") or "").strip()
        source_site_id = normalize_marketplace_site(data.get("site_id") or "MLM")
        source_site_name = marketplace_site_name(source_site_id)
        collection_scope = normalize_collection_scope(
            data.get("collection_scope") or "all"
        )
        if not raw_source_url:
            raw_source_url = build_marketplace_search_url(
                keyword, source_site_id, collection_scope
            )
        source_url, requested_count = validate_collection_request(
            raw_source_url, data.get("requested_count", 20)
        )
        worker_count = normalize_collection_workers(
            data.get("worker_count", DEFAULT_COLLECTION_WORKERS)
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    with _mercado_collection_lock:
        if _mercado_playwright_setup_state.get("running"):
            return jsonify({
                "status": "error",
                "message": "Playwright 登录窗口仍开着，请登录智赢后关闭该窗口，再开始采集",
            }), 409
        if _mercado_collection_state.get("running"):
            return jsonify({
                "status": "error",
                "message": "已有采集任务正在运行，请等待当前任务完成",
            }), 409
        user = session.get("workbench_user") or {}
        try:
            task_id = db_create_mercado_collection_task(
                source_url,
                requested_count,
                str(user.get("display_name") or user.get("username") or ""),
                worker_count=worker_count,
            )
        except Exception as exc:
            logging.exception("创建 Mercado 采集任务失败")
            return jsonify({"status": "error", "message": f"创建采集任务失败：{exc}"}), 500
        _mercado_collection_stop_event.clear()
        _mercado_collection_state.update(
            {
                "running": True,
                "task_id": int(task_id),
                "status": "starting",
                "message": (
                    f"正在启动{source_site_name}"
                    f"{'跨境卖家专区' if collection_scope == 'cross_border' else '全部商品'}"
                    f"采集（并发数 {worker_count}）"
                ),
                "source_url": source_url,
                "keyword": keyword,
                "source_site_id": source_site_id,
                "source_site_name": source_site_name,
                "collection_scope": collection_scope,
                "requested_count": requested_count,
                "worker_count": worker_count,
                "candidate_count": 0,
                "processed_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "elapsed_seconds": 0,
                "current_page": 0,
                "current_item_id": "",
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
            }
        )
        worker = threading.Thread(
            target=_run_mercado_collection_task,
            args=(
                int(task_id),
                source_url,
                requested_count,
                worker_count,
                collection_scope,
            ),
            name=f"mercado-collection-{task_id}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            _mercado_collection_state["running"] = False
            raise
    return jsonify({
        "status": "success",
        "data": {"task_id": int(task_id), **dict(_mercado_collection_state)},
    })


def _run_mercado_playwright_setup():
    try:
        from erp.mercadolibre_playwright_collector import open_playwright_login_setup

        open_playwright_login_setup()
        with _mercado_collection_lock:
            _mercado_playwright_setup_state.update({
                "running": False,
                "status": "completed",
                "message": "采集浏览器已关闭，可以开始采集",
            })
    except Exception as exc:
        logging.exception("打开 Playwright 智赢登录窗口失败")
        with _mercado_collection_lock:
            _mercado_playwright_setup_state.update({
                "running": False,
                "status": "error",
                "message": f"打开采集浏览器失败：{exc}",
            })


@app.route('/api/mercado-collection/playwright-setup', methods=['POST'])
@login_required
def api_open_mercado_playwright_setup():
    with _mercado_collection_lock:
        if _mercado_collection_state.get("running"):
            return jsonify({
                "status": "error",
                "message": "采集任务运行中，不能同时打开登录窗口",
            }), 409
        if _mercado_playwright_setup_state.get("running"):
            return jsonify({
                "status": "success",
                "data": dict(_mercado_playwright_setup_state),
            })
        _mercado_playwright_setup_state.update({
            "running": True,
            "status": "starting",
            "message": "正在打开 Playwright 采集浏览器",
        })
        worker = threading.Thread(
            target=_run_mercado_playwright_setup,
            name="mercado-playwright-login-setup",
            daemon=True,
        )
        worker.start()
        return jsonify({
            "status": "success",
            "data": dict(_mercado_playwright_setup_state),
        })


@app.route('/api/mercado-collection/stop', methods=['POST'])
@login_required
def api_stop_mercado_collection():
    with _mercado_collection_lock:
        if not _mercado_collection_state.get("running"):
            return jsonify({"status": "success", "data": dict(_mercado_collection_state)})
        _mercado_collection_stop_event.set()
        _mercado_collection_state["message"] = "正在停止，当前商品处理后将结束"
        data = dict(_mercado_collection_state)
    return jsonify({"status": "success", "data": data})


@app.route('/api/mercado-collection/status', methods=['GET'])
@login_required
def api_mercado_collection_status():
    with _mercado_collection_lock:
        data = dict(_mercado_collection_state)
    task_id = data.get("task_id")
    if task_id:
        try:
            data["task"] = db_get_mercado_collection_task(task_id)
        except Exception as exc:
            data["database_message"] = str(exc)
    task = data.get("task") or {}
    if not data.get("worker_count") and task.get("worker_count"):
        data["worker_count"] = int(task["worker_count"])
    data["elapsed_seconds"] = _mercado_collection_elapsed_seconds(data, task)
    response = jsonify({"status": "success", "data": data})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/mercado-collection/items', methods=['GET', 'DELETE'])
@login_required
def api_mercado_collection_items():
    try:
        if request.method == "DELETE":
            data = request.get_json(silent=True) or {}
            item_ids = data.get("collection_item_ids") or []
            if not isinstance(item_ids, list):
                return jsonify({"status": "error", "message": "collection_item_ids 必须是数组"}), 422
            result = db_delete_mercado_collection_items(item_ids)
            return jsonify({"status": "success", "data": result})
        task_id = request.args.get("task_id")
        result = db_list_mercado_collection_items(
            search=str(request.args.get("search") or "").strip(),
            limit=_parse_int_param(request.args, "limit", 500, 1, 1000),
            offset=_parse_int_param(request.args, "offset", 0, 0, 1000000),
            task_id=int(task_id) if str(task_id or "").strip() else None,
            exclude_added=True,
        )
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("读取 Mercado 采集列表失败")
        return jsonify({"status": "error", "message": f"读取采集列表失败：{exc}"}), 500


@app.route('/api/mercado-products', methods=['GET', 'DELETE'])
@login_required
def api_mercado_products():
    try:
        if request.method == "DELETE":
            with _mercado_publish_lock:
                if _mercado_publish_state.get("running"):
                    return jsonify({
                        "status": "error",
                        "message": "批量上架正在运行，完成后再删除产品",
                    }), 409
            data = request.get_json(silent=True) or {}
            item_ids = data.get("product_item_ids") or []
            if not isinstance(item_ids, list):
                return jsonify({"status": "error", "message": "product_item_ids 必须是数组"}), 422
            result = db_delete_mercado_product_items(item_ids)
            return jsonify({"status": "success", "data": result})
        result = db_list_mercado_product_items(
            search=str(request.args.get("search") or "").strip(),
            limit=_parse_int_param(request.args, "limit", 500, 1, 1000),
            offset=_parse_int_param(request.args, "offset", 0, 0, 1000000),
            source_type=str(request.args.get("source_type") or "").strip(),
            review_status=str(request.args.get("review_status") or "").strip(),
            publish_status=str(request.args.get("publish_status") or "").strip(),
            weight_min=str(request.args.get("weight_min") or "").strip(),
            weight_max=str(request.args.get("weight_max") or "").strip(),
            price_min=str(request.args.get("price_min") or "").strip(),
            price_max=str(request.args.get("price_max") or "").strip(),
            net_proceeds_min=str(request.args.get("net_proceeds_min") or "").strip(),
            net_proceeds_max=str(request.args.get("net_proceeds_max") or "").strip(),
            date_from=str(request.args.get("date_from") or "").strip(),
            date_to=str(request.args.get("date_to") or "").strip(),
        )
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("读取 Mercado 产品列表失败")
        return jsonify({"status": "error", "message": f"读取产品列表失败：{exc}"}), 500


@app.route('/api/mercado-products/add', methods=['POST'])
@login_required
def api_add_mercado_products():
    data = request.get_json(silent=True) or {}
    item_ids = data.get("collection_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "collection_item_ids 必须是数组"}), 422
    try:
        result = db_add_mercado_collection_items_to_products(item_ids)
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("加入 Mercado 产品列表失败")
        return jsonify({"status": "error", "message": f"加入产品列表失败：{exc}"}), 500


@app.route('/api/mercado-products/<int:product_item_id>', methods=['PATCH'])
@login_required
def api_update_mercado_product(product_item_id):
    with _mercado_publish_lock:
        if _mercado_publish_state.get("running"):
            return jsonify({
                "status": "error",
                "message": "批量上架正在运行，完成后再修改产品",
            }), 409
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "产品内容必须是对象"}), 422
    allowed = {
        "title", "description_text", "main_image_url", "category_id", "price",
        "weight_g", "package_length_cm", "package_width_cm", "package_height_cm",
    }
    try:
        result = db_update_mercado_product_item(
            product_item_id,
            {key: value for key, value in data.items() if key in allowed},
        )
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except KeyError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        logging.exception("修改 Mercado 产品内容失败")
        return jsonify({"status": "error", "message": f"修改产品失败：{exc}"}), 500


@app.route('/api/mercado-products/review-status', methods=['POST'])
@login_required
def api_update_mercado_product_review_status():
    data = request.get_json(silent=True) or {}
    item_ids = data.get("product_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "product_item_ids 必须是数组"}), 422
    try:
        result = db_update_mercado_product_review_status(
            item_ids, str(data.get("review_status") or "").strip()
        )
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("更新 Mercado 产品审核状态失败")
        return jsonify({"status": "error", "message": f"更新审核状态失败：{exc}"}), 500


def _mercado_publish_state_update(**changes):
    with _mercado_publish_lock:
        _mercado_publish_state.update(changes)


def _run_mercado_product_publish(
    product_rows, token_id, site_id, site_name, quantity, worker_count, store_name,
    batch_id, created_by, discount_rate, moved_to_collection_count=0,
):
    """Backward-compatible single-target entry point."""
    return _run_mercado_product_publish_targets(
        product_rows,
        targets=[{
            "token_id": int(token_id),
            "store_name": str(store_name),
            "site_id": str(site_id),
            "site_name": str(site_name),
            "discount_rate": float(discount_rate),
        }],
        quantity=quantity,
        worker_count=worker_count,
        batch_id=batch_id,
        created_by=created_by,
        moved_to_collection_count=moved_to_collection_count,
    )


def _run_mercado_product_publish_targets(
    product_rows, targets, quantity, worker_count, batch_id, created_by,
    moved_to_collection_count=0,
):
    from erp.mercadolibre_batch_publish import publish_product_batch

    rows = [dict(row) for row in product_rows or []]
    target_rows = [dict(target) for target in targets or []]
    target_count = len(target_rows)
    total_requested = sum(
        len(target.get("product_rows") or rows) for target in target_rows
    )
    processed = published = failed = skipped_published = 0
    all_results = []
    stage_duration_totals: dict[str, float] = {}
    stage_sample_count = 0
    started_monotonic = time.monotonic()

    def timing_metrics(done_count):
        elapsed = max(0.0, time.monotonic() - started_monotonic)
        done = max(0, int(done_count or 0))
        attempted_done = max(0, done - skipped_published)
        rate_per_second = (
            attempted_done / elapsed
        ) if attempted_done and elapsed else 0.0
        remaining = max(0, total_requested - done)
        return {
            "elapsed_seconds": round(elapsed, 1),
            "average_seconds_per_item": round(
                elapsed / attempted_done, 2
            ) if attempted_done else 0,
            "items_per_minute": round(rate_per_second * 60, 2),
            "estimated_remaining_seconds": round(
                remaining / rate_per_second, 1
            ) if rate_per_second else 0,
        }

    for target_index, target in enumerate(target_rows, start=1):
        token_id = int(target.get("token_id") or 0)
        site_id = str(target.get("site_id") or "")
        site_name = str(target.get("site_name") or site_id)
        store_name = str(target.get("store_name") or token_id)
        discount_rate = float(target.get("discount_rate") or 0)
        current_rows = [dict(row) for row in (target.get("product_rows") or rows)]
        current_quantity = int(target.get("quantity") or quantity)
        try:
            published_product_ids = set(db_get_published_mercado_product_item_ids(
                [int(row.get("id") or 0) for row in current_rows],
                token_id=token_id,
                site_id=site_id,
            ))
        except Exception:
            logging.exception(
                "读取历史成功上架记录失败，将继续当前组合: token_id=%s site_id=%s",
                token_id,
                site_id,
            )
            published_product_ids = set()
        target_rows_to_publish = [
            row for row in current_rows
            if int(row.get("id") or 0) not in published_product_ids
        ]
        target_skipped = len(current_rows) - len(target_rows_to_publish)
        skipped_published += target_skipped
        processed += target_skipped
        base_processed = processed
        base_published = published
        base_failed = failed

        def on_progress(info, *, _target_index=target_index):
            current = int(info.get("current") or 0)
            current_published = int(info.get("published_count") or 0)
            current_failed = int(info.get("failed_count") or 0)
            aggregate_processed = base_processed + current
            _mercado_publish_state_update(
                processed_count=aggregate_processed,
                published_count=base_published + current_published,
                failed_count=base_failed + current_failed,
                skipped_published_count=skipped_published,
                completed_target_count=_target_index - 1,
                worker_count=int(info.get("worker_count") or worker_count),
                current_item_id=str(info.get("source_item_id") or ""),
                average_stage_seconds=dict(
                    info.get("average_stage_seconds") or {}
                ),
                message=(
                    f"组合 {_target_index}/{target_count} · {store_name} · {site_name}"
                ),
                **timing_metrics(aggregate_processed),
            )

        try:
            target_batch_id = f"{batch_id}-{target_index}"
            if target_rows_to_publish:
                result = publish_product_batch(
                    target_rows_to_publish,
                    token_id=token_id,
                    site_id=site_id,
                    quantity=current_quantity,
                    workers=int(worker_count),
                    discount_rate=discount_rate,
                    update_state=db_update_mercado_product_publish_state,
                    on_progress=on_progress,
                    batch_id=target_batch_id,
                    created_by=str(created_by),
                    create_records=db_create_mercado_product_publish_records,
                    update_record=db_update_mercado_product_publish_record,
                )
                target_requested = int(
                    result.get("requested_count") or len(target_rows_to_publish)
                )
                target_published = int(result.get("published_count") or 0)
                target_failed = int(result.get("failed_count") or 0)
                target_results = list(result.get("results") or [])
                target_stage_averages = dict(
                    result.get("average_stage_seconds") or {}
                )
            else:
                target_requested = target_published = target_failed = 0
                target_results = []
                target_stage_averages = {}
        except Exception as exc:
            logging.exception(
                "Mercado 产品上架组合失败: token_id=%s site_id=%s",
                token_id,
                site_id,
            )
            target_requested = len(target_rows_to_publish)
            target_published = 0
            target_failed = len(target_rows_to_publish)
            target_results = [{
                "product_id": int(row.get("id") or 0),
                "source_item_id": str(row.get("source_item_id") or ""),
                "status": "failed",
                "published_item_id": "",
                "message": f"上架组合启动失败：{exc}"[:2000],
            } for row in target_rows_to_publish]
            target_stage_averages = {}

        processed += target_requested
        published += target_published
        failed += target_failed
        if target_results:
            sample_size = len(target_results)
            stage_sample_count += sample_size
            for stage, average in target_stage_averages.items():
                try:
                    stage_duration_totals[str(stage)] = (
                        stage_duration_totals.get(str(stage), 0.0)
                        + float(average or 0) * sample_size
                    )
                except (TypeError, ValueError):
                    continue
        aggregate_stage_averages = {
            stage: round(total / max(1, stage_sample_count), 4)
            for stage, total in stage_duration_totals.items()
        }
        for item in target_results:
            all_results.append({
                **dict(item),
                "target_index": target_index,
                "token_id": token_id,
                "store_name": store_name,
                "site_id": site_id,
                "site_name": site_name,
                "discount_rate": discount_rate,
            })
        _mercado_publish_state_update(
            processed_count=processed,
            published_count=published,
            failed_count=failed,
            skipped_published_count=skipped_published,
            completed_target_count=target_index,
            current_item_id="",
            average_stage_seconds=aggregate_stage_averages,
            results=list(all_results),
            message=f"已完成组合 {target_index}/{target_count} · {store_name} · {site_name}",
            **timing_metrics(processed),
        )

    if failed == 0:
        status = "completed"
    elif published:
        status = "partial"
    else:
        status = "error"
    message = f"批量上架完成：{target_count} 个账号-站点组合"
    if moved_to_collection_count:
        message += f"；已忽略并移回采集列表 {moved_to_collection_count} 件不可上架商品"
    _mercado_publish_state_update(
        running=False,
        status=status,
        message=message,
        requested_count=total_requested,
        processed_count=processed,
        published_count=published,
        failed_count=failed,
        moved_to_collection_count=int(moved_to_collection_count or 0),
        skipped_published_count=skipped_published,
        current_item_id="",
        completed_target_count=target_count,
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        results=list(all_results),
        average_stage_seconds={
            stage: round(total / max(1, stage_sample_count), 4)
            for stage, total in stage_duration_totals.items()
        },
        batch_id=str(batch_id),
        **timing_metrics(processed),
    )


@app.route('/api/mercado-products/publish', methods=['POST'])
@login_required
def api_publish_mercado_products():
    data = request.get_json(silent=True) or {}
    item_ids = data.get("product_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "product_item_ids 必须是数组"}), 422
    with _mercado_publish_lock:
        if _mercado_publish_state.get("running"):
            return jsonify({
                "status": "error",
                "message": "已有批量上架任务正在运行",
            }), 409
    try:
        from erp.mercadolibre_translation import (
            marketplace_site_name,
            normalize_marketplace_site,
        )
        from erp.mercadolibre_batch_publish import (
            product_publish_issues,
            site_discount_rate,
            validate_publishable_products,
        )

        selection_mode = str(data.get("selection_mode") or "accounts").strip().lower()
        if selection_mode not in {"accounts", "groups"}:
            raise ValueError("上架选择方式必须是账号或分组")

        raw_token_ids = data.get("token_ids")
        if raw_token_ids is None:
            raw_token_ids = [data.get("token_id")]
        if not isinstance(raw_token_ids, list):
            raise ValueError("token_ids 必须是数组")
        token_ids = []
        for value in raw_token_ids:
            token_id_value = int(value or 0)
            if token_id_value > 0 and token_id_value not in token_ids:
                token_ids.append(token_id_value)

        raw_group_names = data.get("group_names") or []
        if not isinstance(raw_group_names, list):
            raise ValueError("group_names 必须是数组")
        group_names = []
        for value in raw_group_names:
            group_name = str(value or "").strip()
            if group_name and group_name not in group_names:
                group_names.append(group_name)

        raw_site_ids = data.get("site_ids")
        if raw_site_ids is None:
            raw_site_ids = [data.get("site_id") or "MLM"]
        if not isinstance(raw_site_ids, list):
            raise ValueError("site_ids 必须是数组")
        site_ids = []
        for value in raw_site_ids:
            site_id_value = normalize_marketplace_site(value)
            if site_id_value not in site_ids:
                site_ids.append(site_id_value)
        if not site_ids:
            raise ValueError("请选择至少一个目标站点")
        if selection_mode == "accounts" and not token_ids:
            raise ValueError("请选择至少一个要上架的授权账号")
        if selection_mode == "groups" and not group_names:
            raise ValueError("请选择至少一个账号分组")

        token_id = token_ids[0] if token_ids else None
        site_id = site_ids[0]
        site_name = marketplace_site_name(site_id)
        raw_quantity = data.get("quantity")
        raw_worker_count = data.get("worker_count")
        quantity = int(500 if raw_quantity in (None, "") else raw_quantity)
        worker_count = int(10 if raw_worker_count in (None, "") else raw_worker_count)
        if quantity < 1 or quantity > 9999:
            raise ValueError("上架库存必须在 1-9999 之间")
        if worker_count < 1:
            raise ValueError("上架并发必须是大于 0 的整数")
        rows = db_get_mercado_product_items_by_ids(item_ids)
        if len(rows) != len({int(value) for value in item_ids}):
            raise ValueError("部分勾选产品已不存在，请刷新列表后重试")
        blocked_rows = []
        publish_rows = []
        for row in rows:
            issues = product_publish_issues(row)
            if issues:
                blocked_rows.append((row, issues))
            else:
                publish_rows.append(row)
        moved_to_collection_count = 0
        if blocked_rows:
            reason_counts: dict[str, int] = {}
            for _row, issues in blocked_rows:
                for issue in issues:
                    reason_counts[issue] = reason_counts.get(issue, 0) + 1
            reason_summary = "；".join(
                f"{reason} {count} 件" for reason, count in reason_counts.items()
            )
            move_result = db_move_mercado_product_items_to_collection(
                [int(row.get("id") or 0) for row, _issues in blocked_rows],
                reason=reason_summary,
            )
            moved_to_collection_count = int(
                move_result.get("moved") or move_result.get("deleted") or 0
            )
        rows = publish_rows
        if not rows:
            message = (
                f"已忽略并移回采集列表 {moved_to_collection_count} 件不可上架商品，"
                "本次没有可上架产品"
            )
            with _mercado_publish_lock:
                _mercado_publish_state.update({
                    "running": False,
                    "batch_id": "",
                    "status": "completed",
                    "message": message,
                    "selection_mode": selection_mode,
                    "token_id": token_id,
                    "token_ids": token_ids,
                    "group_names": group_names,
                    "site_id": site_id,
                    "site_ids": site_ids,
                    "site_name": site_name,
                    "target_count": 0,
                    "completed_target_count": 0,
                    "skipped_target_count": 0,
                    "quantity": quantity,
                    "worker_count": 0,
                    "requested_count": 0,
                    "processed_count": 0,
                    "published_count": 0,
                    "failed_count": 0,
                    "moved_to_collection_count": moved_to_collection_count,
                    "skipped_published_count": 0,
                    "elapsed_seconds": 0,
                    "average_seconds_per_item": 0,
                    "items_per_minute": 0,
                    "estimated_remaining_seconds": 0,
                    "average_stage_seconds": {},
                    "current_item_id": "",
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "results": [],
                })
                state = dict(_mercado_publish_state)
            return jsonify({"status": "success", "data": state})
        validate_publishable_products(rows)
        token_rows = list((bit_db_api.list_mercado_store_tokens() or {}).get("rows") or [])
        token_by_id = {
            int(row.get("id") or 0): row
            for row in token_rows
            if int(row.get("id") or 0) > 0
        }
        if selection_mode == "accounts":
            missing_token_ids = [value for value in token_ids if value not in token_by_id]
            if missing_token_ids:
                raise ValueError(
                    "部分授权账号不存在，请刷新后重试："
                    + "、".join(str(value) for value in missing_token_ids)
                )
            candidate_tokens = [token_by_id[value] for value in token_ids]
        else:
            candidate_tokens = [
                row for row in token_rows
                if row.get("status") != "expired" or row.get("has_refresh_token")
            ]

        targets = []
        skipped_target_count = 0
        for token in candidate_tokens:
            current_token_id = int(token.get("id") or 0)
            token_site_id = str(token.get("site_id") or "").strip().upper()
            store_name = str(
                token.get("display_name") or token.get("nickname") or current_token_id
            )
            settings_by_site = {
                str(setting.get("site_id") or "").strip().upper(): setting
                for setting in (token.get("site_settings") or [])
                if str(setting.get("site_id") or "").strip()
            }
            for current_site_id in site_ids:
                compatible = (
                    not token_site_id
                    or token_site_id == "CBT"
                    or token_site_id == current_site_id
                )
                if not compatible:
                    if selection_mode == "accounts":
                        skipped_target_count += 1
                    continue
                site_setting = settings_by_site.get(current_site_id) or {}
                configured_group = str(site_setting.get("group_name") or "").strip()
                normalized_group = configured_group or "__ungrouped__"
                if selection_mode == "groups" and normalized_group not in group_names:
                    continue
                targets.append({
                    "token_id": current_token_id,
                    "store_name": store_name,
                    "site_id": current_site_id,
                    "site_name": marketplace_site_name(current_site_id),
                    "group_name": configured_group,
                    "discount_rate": float(site_discount_rate(token, current_site_id)),
                })

        if not targets:
            if (
                selection_mode == "accounts"
                and len(token_ids) == 1
                and len(site_ids) == 1
                and token_ids[0] in token_by_id
            ):
                selected_token_site = str(
                    token_by_id[token_ids[0]].get("site_id") or ""
                ).strip().upper()
                if selected_token_site and selected_token_site != "CBT":
                    raise ValueError(
                        f"所选店铺属于 {selected_token_site} 站点，不能发布到 {site_ids[0]}；"
                        "请选择 Global Selling 店铺或对应本地站点"
                    )
            if selection_mode == "groups":
                raise ValueError("所选分组和站点没有可用的账号-站点组合")
            raise ValueError("所选账号和站点没有兼容的上架组合")

        target_token_ids = list(dict.fromkeys(
            int(target["token_id"]) for target in targets
        ))
        target_site_ids = list(dict.fromkeys(
            str(target["site_id"]) for target in targets
        ))
        target_store_names = list(dict.fromkeys(
            str(target["store_name"]) for target in targets
        ))
        target_site_names = list(dict.fromkeys(
            str(target["site_name"]) for target in targets
        ))
        store_name = (
            target_store_names[0]
            if len(target_store_names) == 1
            else f"{len(target_store_names)} 个账号"
        )
        site_name = (
            target_site_names[0]
            if len(target_site_names) == 1
            else f"{len(target_site_names)} 个站点"
        )
        discount_rate = (
            float(targets[0]["discount_rate"])
            if len(targets) == 1 else None
        )
        current_user = get_current_workbench_user() or {}
        created_by = str(
            current_user.get("display_name") or current_user.get("username") or ""
        )[:128]
        batch_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("准备 Mercado 批量上架失败")
        return jsonify({"status": "error", "message": f"准备批量上架失败：{exc}"}), 500

    with _mercado_publish_lock:
        if _mercado_publish_state.get("running"):
            return jsonify({
                "status": "error",
                "message": "已有批量上架任务正在运行",
            }), 409
        _mercado_publish_state.update({
            "running": True,
            "batch_id": batch_id,
            "status": "running",
            "message": (
                f"准备使用 {min(worker_count, len(rows))} 个线程上架 "
                f"{len(rows)} 件产品到 {len(targets)} 个账号-站点组合 · "
                "各组合按对应站点折扣计算净收益"
                + (
                    f"；已忽略并移回采集列表 {moved_to_collection_count} 件不可上架商品"
                    if moved_to_collection_count else ""
                )
            ),
            "selection_mode": selection_mode,
            "token_id": token_id,
            "token_ids": target_token_ids,
            "group_names": group_names,
            "store_name": store_name,
            "site_id": target_site_ids[0] if len(target_site_ids) == 1 else "",
            "site_ids": target_site_ids,
            "site_name": site_name,
            "target_count": len(targets),
            "completed_target_count": 0,
            "skipped_target_count": skipped_target_count,
            "quantity": quantity,
            "worker_count": min(worker_count, len(rows)),
            "discount_rate": discount_rate,
            "requested_count": len(rows) * len(targets),
            "processed_count": 0,
            "published_count": 0,
            "failed_count": 0,
            "moved_to_collection_count": moved_to_collection_count,
            "skipped_published_count": 0,
            "elapsed_seconds": 0,
            "average_seconds_per_item": 0,
            "items_per_minute": 0,
            "estimated_remaining_seconds": 0,
            "average_stage_seconds": {},
            "current_item_id": "",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "results": [],
        })
        thread = threading.Thread(
            target=_run_mercado_product_publish_targets,
            args=(
                rows,
                targets,
                quantity,
                worker_count,
                batch_id,
                created_by,
                moved_to_collection_count,
            ),
            name=f"mercado-publish-{batch_id}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            _mercado_publish_state.update(
                running=False,
                status="error",
                message=f"批量上架线程启动失败：{exc}",
            )
            raise
        state = dict(_mercado_publish_state)
    return jsonify({"status": "success", "data": state})


@app.route('/api/mercado-products/publish/status', methods=['GET'])
@login_required
def api_mercado_product_publish_status():
    with _mercado_publish_lock:
        state = {**_mercado_publish_state, "results": list(_mercado_publish_state.get("results") or [])}
    response = jsonify({"status": "success", "data": state})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/mercado-shipping-rates', methods=['GET'])
@login_required
def api_mercado_shipping_rates():
    try:
        from erp.mercadolibre_shipping_rate_cards import OfficialShippingRateCardStore

        site_id = str(request.args.get("site_id") or "").strip().upper()
        result = OfficialShippingRateCardStore().list_rates(site_id)
        result["refresh"] = _mercado_shipping_rate_refresh_snapshot()
        response = jsonify({"status": "success", "data": result})
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        logging.exception("读取 Mercado 官方运费表失败")
        return jsonify({"status": "error", "message": f"读取官方运费表失败：{exc}"}), 500


@app.route('/api/mercado-shipping-rates/refresh', methods=['POST'])
@login_required
def api_refresh_mercado_shipping_rates():
    started = _start_mercado_shipping_rate_refresh()
    data = _mercado_shipping_rate_refresh_snapshot()
    return jsonify({
        "status": "success",
        "message": "已开始更新 Global Selling 官方最新标准" if started else "官方标准更新正在运行",
        "data": data,
    }), (202 if started else 200)


@app.route('/api/mercado-publish-records', methods=['GET'])
@login_required
def api_mercado_product_publish_records():
    try:
        result = db_list_mercado_product_publish_records(
            search=str(request.args.get("search") or "").strip(),
            status=str(request.args.get("status") or "").strip(),
            store_name=str(request.args.get("store_name") or "").strip(),
            site_id=str(request.args.get("site_id") or "").strip(),
            limit=_parse_int_param(request.args, "limit", 500, 1, 1000),
            offset=_parse_int_param(request.args, "offset", 0, 0, 1000000),
        )
        with _mercado_publish_lock:
            result["publish_running"] = bool(_mercado_publish_state.get("running"))
        response = jsonify({"status": "success", "data": result})
        response.headers["Cache-Control"] = "no-store"
        return response
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("读取 Mercado 产品上架记录失败")
        return jsonify({"status": "error", "message": f"读取产品上架记录失败：{exc}"}), 500


@app.route('/api/mercado-publish-records/retry', methods=['POST'])
@login_required
def api_retry_mercado_product_publish_records():
    data = request.get_json(silent=True) or {}
    record_ids = data.get("record_ids") or []
    if not isinstance(record_ids, list):
        return jsonify({"status": "error", "message": "record_ids 必须是数组"}), 422
    with _mercado_publish_lock:
        if _mercado_publish_state.get("running"):
            return jsonify({
                "status": "error",
                "message": "已有批量上架任务正在运行，完成后再重新上架",
            }), 409
    try:
        from erp.mercadolibre_batch_publish import (
            site_discount_rate,
            validate_publishable_products,
        )
        from erp.mercadolibre_collection_store import (
            PRODUCT_PUBLISH_RETRYABLE_STATUSES,
        )
        from erp.mercadolibre_translation import (
            marketplace_site_name,
            normalize_marketplace_site,
        )

        normalized_record_ids = list(dict.fromkeys(
            int(value) for value in record_ids if int(value) > 0
        ))
        if not normalized_record_ids:
            raise ValueError("请至少勾选一条可重新上架的记录")
        records = db_get_mercado_product_publish_records_by_ids(normalized_record_ids)
        if len(records) != len(normalized_record_ids):
            raise ValueError("部分上架记录已不存在，请刷新后重试")
        blocked = [
            record for record in records
            if str(record.get("status") or "") not in PRODUCT_PUBLISH_RETRYABLE_STATUSES
        ]
        if blocked:
            raise ValueError("只有上架暂停或上架失败的记录可以重新上架")

        # A product may have several failed attempts. Keep only the newest selected
        # attempt for the same product/account/site to prevent duplicate listings.
        retry_records = []
        seen_product_targets = set()
        for record in records:
            key = (
                int(record.get("product_item_id") or 0),
                int(record.get("token_id") or 0),
                str(record.get("site_id") or "").strip().upper(),
            )
            if min(key[0], key[1]) <= 0 or not key[2]:
                raise ValueError("所选记录缺少产品、账号或站点信息")
            if key in seen_product_targets:
                continue
            seen_product_targets.add(key)
            retry_records.append(record)
        duplicate_selection_count = len(records) - len(retry_records)

        product_ids = list(dict.fromkeys(
            int(record.get("product_item_id") or 0) for record in retry_records
        ))
        products = db_get_mercado_product_items_by_ids(product_ids)
        product_by_id = {int(row.get("id") or 0): dict(row) for row in products}
        missing_product_ids = [value for value in product_ids if value not in product_by_id]
        if missing_product_ids:
            raise ValueError("部分记录对应的产品已从产品列表删除，无法重新上架")
        validate_publishable_products(products)

        token_rows = list((bit_db_api.list_mercado_store_tokens() or {}).get("rows") or [])
        token_by_id = {
            int(row.get("id") or 0): dict(row)
            for row in token_rows if int(row.get("id") or 0) > 0
        }
        grouped_targets: dict[tuple[int, str, int], dict] = {}
        for record in retry_records:
            token_id = int(record.get("token_id") or 0)
            token = token_by_id.get(token_id)
            if not token:
                raise ValueError(f"上架账号 {token_id} 已不存在，请重新授权后再试")
            site_id = normalize_marketplace_site(record.get("site_id") or "")
            token_site_id = str(token.get("site_id") or "").strip().upper()
            if token_site_id and token_site_id != "CBT" and token_site_id != site_id:
                raise ValueError(
                    f"账号 {token_id} 属于 {token_site_id} 站点，不能重新发布到 {site_id}"
                )
            quantity = int(record.get("quantity") or 1)
            if quantity < 1 or quantity > 9999:
                raise ValueError("历史记录中的上架库存无效")
            group_key = (token_id, site_id, quantity)
            target = grouped_targets.setdefault(group_key, {
                "token_id": token_id,
                "store_name": str(
                    token.get("display_name") or token.get("nickname")
                    or record.get("store_name") or token_id
                ),
                "site_id": site_id,
                "site_name": marketplace_site_name(site_id),
                "discount_rate": float(site_discount_rate(token, site_id)),
                "quantity": quantity,
                "product_rows": [],
            })
            product = product_by_id[int(record.get("product_item_id") or 0)]
            target["product_rows"].append(product)

        targets = list(grouped_targets.values())
        requested_count = sum(len(target["product_rows"]) for target in targets)
        worker_count = int(data.get("worker_count") or 10)
        if worker_count < 1:
            raise ValueError("重新上架并发必须是大于 0 的整数")
        current_user = get_current_workbench_user() or {}
        created_by = str(
            current_user.get("display_name") or current_user.get("username") or ""
        )[:128]
        batch_id = f"retry-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
        token_ids = list(dict.fromkeys(int(target["token_id"]) for target in targets))
        site_ids = list(dict.fromkeys(str(target["site_id"]) for target in targets))
        quantities = list(dict.fromkeys(int(target["quantity"]) for target in targets))
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("准备重新上架 Mercado 产品失败")
        return jsonify({"status": "error", "message": f"准备重新上架失败：{exc}"}), 500

    with _mercado_publish_lock:
        if _mercado_publish_state.get("running"):
            return jsonify({
                "status": "error",
                "message": "已有批量上架任务正在运行，完成后再重新上架",
            }), 409
        _mercado_publish_state.update({
            "running": True,
            "batch_id": batch_id,
            "status": "running",
            "message": f"正在重新上架 {requested_count} 件产品",
            "selection_mode": "retry",
            "token_id": token_ids[0] if len(token_ids) == 1 else None,
            "token_ids": token_ids,
            "group_names": [],
            "store_name": (
                targets[0]["store_name"] if len(targets) == 1
                else f"{len(token_ids)} 个账号"
            ),
            "site_id": site_ids[0] if len(site_ids) == 1 else "",
            "site_ids": site_ids,
            "site_name": (
                targets[0]["site_name"] if len(site_ids) == 1
                else f"{len(site_ids)} 个站点"
            ),
            "target_count": len(targets),
            "completed_target_count": 0,
            "skipped_target_count": 0,
            "quantity": quantities[0] if len(quantities) == 1 else 0,
            "worker_count": min(worker_count, requested_count),
            "discount_rate": None,
            "requested_count": requested_count,
            "processed_count": 0,
            "published_count": 0,
            "failed_count": 0,
            "moved_to_collection_count": 0,
            "skipped_published_count": 0,
            "duplicate_selection_count": duplicate_selection_count,
            "elapsed_seconds": 0,
            "average_seconds_per_item": 0,
            "items_per_minute": 0,
            "estimated_remaining_seconds": 0,
            "current_item_id": "",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "results": [],
        })
        thread = threading.Thread(
            target=_run_mercado_product_publish_targets,
            args=([], targets, 1, worker_count, batch_id, created_by, 0),
            name=f"mercado-publish-retry-{batch_id}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            _mercado_publish_state.update(
                running=False,
                status="error",
                message=f"重新上架线程启动失败：{exc}",
            )
            raise
        state = dict(_mercado_publish_state)
    return jsonify({"status": "success", "data": state})


@app.route('/api/db/mercado-collection/tasks', methods=['POST'])
@internal_api_required
def api_db_create_mercado_collection_task():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        task_id = db_create_mercado_collection_task(
            data.get("source_url", ""),
            data.get("requested_count", 20),
            data.get("created_by", ""),
        )
        return jsonify({"status": "success", "data": {"task_id": int(task_id)}})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/db/mercado-collection/tasks/<int:task_id>', methods=['GET', 'PATCH'])
@internal_api_required
def api_db_mercado_collection_task(task_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    if request.method == "GET":
        row = db_get_mercado_collection_task(task_id)
        if not row:
            return jsonify({"status": "error", "message": "采集任务不存在"}), 404
        return jsonify({"status": "success", "data": row})
    changes = request.get_json(silent=True) or {}
    allowed = {
        "status", "message", "collected_count", "completed_count",
        "failed_count", "current_page", "started", "finished",
    }
    db_update_mercado_collection_task(
        task_id, **{key: value for key, value in changes.items() if key in allowed}
    )
    return jsonify({"status": "success", "data": {"task_id": task_id}})


@app.route('/api/db/mercado-collection/items', methods=['GET', 'POST'])
@internal_api_required
def api_db_mercado_collection_items():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    if request.method == "GET":
        task_id = request.args.get("task_id")
        result = db_list_mercado_collection_items(
            search=str(request.args.get("search") or "").strip(),
            limit=_parse_int_param(request.args, "limit", 500, 1, 1000),
            offset=_parse_int_param(request.args, "offset", 0, 0, 1000000),
            task_id=int(task_id) if str(task_id or "").strip() else None,
        )
        return jsonify({"status": "success", "data": result})
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        return jsonify({"status": "error", "message": "rows 必须是数组"}), 422
    count = db_upsert_mercado_collection_items(int(data.get("task_id")), rows)
    return jsonify({"status": "success", "data": {"count": count}})


@app.route('/api/db/mercado-products', methods=['GET'])
@internal_api_required
def api_db_mercado_products():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    result = db_list_mercado_product_items(
        search=str(request.args.get("search") or "").strip(),
        limit=_parse_int_param(request.args, "limit", 500, 1, 1000),
        offset=_parse_int_param(request.args, "offset", 0, 0, 1000000),
        source_type=str(request.args.get("source_type") or "").strip(),
        review_status=str(request.args.get("review_status") or "").strip(),
        publish_status=str(request.args.get("publish_status") or "").strip(),
        weight_min=str(request.args.get("weight_min") or "").strip(),
        weight_max=str(request.args.get("weight_max") or "").strip(),
        price_min=str(request.args.get("price_min") or "").strip(),
        price_max=str(request.args.get("price_max") or "").strip(),
        net_proceeds_min=str(request.args.get("net_proceeds_min") or "").strip(),
        net_proceeds_max=str(request.args.get("net_proceeds_max") or "").strip(),
        date_from=str(request.args.get("date_from") or "").strip(),
        date_to=str(request.args.get("date_to") or "").strip(),
    )
    return jsonify({"status": "success", "data": result})


@app.route('/api/db/mercado-products/<int:product_item_id>', methods=['PATCH'])
@internal_api_required
def api_db_update_mercado_product(product_item_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "产品内容必须是对象"}), 422
    try:
        result = db_update_mercado_product_item(product_item_id, data)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except KeyError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    return jsonify({"status": "success", "data": result})


@app.route('/api/db/mercado-publish-records', methods=['GET'])
@internal_api_required
def api_db_mercado_product_publish_records():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    result = db_list_mercado_product_publish_records(
        search=str(request.args.get("search") or "").strip(),
        status=str(request.args.get("status") or "").strip(),
        store_name=str(request.args.get("store_name") or "").strip(),
        site_id=str(request.args.get("site_id") or "").strip(),
        limit=_parse_int_param(request.args, "limit", 500, 1, 1000),
        offset=_parse_int_param(request.args, "offset", 0, 0, 1000000),
    )
    return jsonify({"status": "success", "data": result})


@app.route('/api/db/mercado-publish-records/published-product-ids', methods=['POST'])
@internal_api_required
def api_db_published_mercado_product_item_ids():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    item_ids = data.get("product_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "product_item_ids 必须是数组"}), 422
    try:
        published_ids = db_get_published_mercado_product_item_ids(
            item_ids,
            token_id=int(data.get("token_id") or 0),
            site_id=str(data.get("site_id") or ""),
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({
        "status": "success",
        "data": {"product_item_ids": published_ids},
    })


@app.route('/api/db/mercado-publish-records/by-ids', methods=['POST'])
@internal_api_required
def api_db_mercado_product_publish_records_by_ids():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    record_ids = data.get("record_ids") or []
    if not isinstance(record_ids, list):
        return jsonify({"status": "error", "message": "record_ids 必须是数组"}), 422
    try:
        rows = db_get_mercado_product_publish_records_by_ids(record_ids)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "data": {"rows": rows}})


@app.route('/api/db/mercado-publish-records/bulk', methods=['POST'])
@internal_api_required
def api_db_create_mercado_product_publish_records():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        return jsonify({"status": "error", "message": "rows 必须是数组"}), 422
    try:
        record_ids = db_create_mercado_product_publish_records(
            rows,
            batch_id=data.get("batch_id", ""),
            token_id=data.get("token_id", 0),
            store_name=data.get("store_name", ""),
            site_id=data.get("site_id", ""),
            site_name=data.get("site_name", ""),
            quantity=data.get("quantity", 1),
            created_by=data.get("created_by", ""),
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({
        "status": "success",
        "data": {"record_ids": {str(key): value for key, value in record_ids.items()}},
    })


@app.route('/api/db/mercado-publish-records/<int:record_id>', methods=['PATCH'])
@internal_api_required
def api_db_update_mercado_product_publish_record(record_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    allowed = {
        "status", "published_item_id", "failure_reason", "result", "started", "finished",
    }
    try:
        db_update_mercado_product_publish_record(
            record_id,
            **{key: value for key, value in data.items() if key in allowed},
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except KeyError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    return jsonify({"status": "success", "data": {"record_id": record_id}})


@app.route('/api/db/mercado-products/review-status', methods=['POST'])
@internal_api_required
def api_db_update_mercado_product_review_status():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    item_ids = data.get("product_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "product_item_ids 必须是数组"}), 422
    try:
        result = db_update_mercado_product_review_status(
            item_ids, str(data.get("review_status") or "").strip()
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "data": result})


@app.route('/api/db/mercado-products/move-to-collection', methods=['POST'])
@internal_api_required
def api_db_move_mercado_products_to_collection():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    item_ids = data.get("product_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "product_item_ids 必须是数组"}), 422
    try:
        result = db_move_mercado_product_items_to_collection(
            item_ids,
            reason=str(data.get("reason") or "不可上架"),
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "data": result})


@app.route('/api/db/mercado-products/add', methods=['POST'])
@internal_api_required
def api_db_add_mercado_products():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    item_ids = data.get("collection_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "collection_item_ids 必须是数组"}), 422
    result = db_add_mercado_collection_items_to_products(item_ids)
    return jsonify({"status": "success", "data": result})


@app.route('/api/db/mercado-collection/items/delete', methods=['POST'])
@internal_api_required
def api_db_delete_mercado_collection_items():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    item_ids = data.get("collection_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "collection_item_ids 必须是数组"}), 422
    return jsonify({"status": "success", "data": db_delete_mercado_collection_items(item_ids)})


@app.route('/api/db/mercado-products/delete', methods=['POST'])
@internal_api_required
def api_db_delete_mercado_product_items():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    item_ids = data.get("product_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "product_item_ids 必须是数组"}), 422
    return jsonify({"status": "success", "data": db_delete_mercado_product_items(item_ids)})


@app.route('/api/db/mercado-products/by-ids', methods=['POST'])
@internal_api_required
def api_db_get_mercado_product_items_by_ids():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    item_ids = data.get("product_item_ids") or []
    if not isinstance(item_ids, list):
        return jsonify({"status": "error", "message": "product_item_ids 必须是数组"}), 422
    rows = db_get_mercado_product_items_by_ids(item_ids)
    return jsonify({"status": "success", "data": {"rows": rows}})


@app.route('/api/db/mercado-products/<int:product_item_id>/publish-state', methods=['PATCH'])
@internal_api_required
def api_db_update_mercado_product_publish_state(product_item_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    allowed = {
        "status", "store_name", "token_id", "published_item_id",
        "error_message", "result", "finished",
    }
    db_update_mercado_product_publish_state(
        product_item_id,
        **{key: value for key, value in data.items() if key in allowed},
    )
    return jsonify({"status": "success", "data": {"product_item_id": product_item_id}})


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


def _mercado_token_error_response(exc):
    if isinstance(exc, KeyError):
        return jsonify({"status": "error", "message": str(exc.args[0])}), 404
    if isinstance(
        exc,
        (
            ValueError,
            mercado_tokens.MercadoTokenError,
            mercado_reputation.MercadoReputationError,
        ),
    ):
        return jsonify({"status": "error", "message": str(exc)}), 400
    logging.exception("店铺授权操作失败")
    return jsonify({"status": "error", "message": f"店铺授权操作失败：{exc}"}), 500


def _mercado_communication_error_response(exc):
    if isinstance(exc, KeyError):
        return jsonify({"status": "error", "message": str(exc.args[0])}), 404
    if isinstance(exc, ValueError):
        return jsonify({"status": "error", "message": str(exc)}), 400
    if isinstance(exc, mercado_communications.MercadoCommunicationError):
        status_code = int(exc.status_code or 502)
        if status_code not in (400, 401, 403, 404, 409, 422, 429):
            status_code = 502
        return jsonify({
            "status": "error",
            "message": str(exc),
            "model_6_restricted": bool(exc.model_6_restricted),
        }), status_code
    logging.exception("美客多客户消息操作失败")
    return jsonify({"status": "error", "message": f"美客多客户消息操作失败：{exc}"}), 500


@app.route('/api/db/mercado-tokens/authorization', methods=['GET'])
@internal_api_required
def api_db_mercado_token_authorization():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    return jsonify({
        "status": "success",
        "data": bit_db_api.get_mercado_token_authorization_info(),
    })


@app.route('/api/db/mercado-tokens', methods=['GET'])
@internal_api_required
def api_db_list_mercado_tokens():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    return jsonify({"status": "success", "data": bit_db_api.list_mercado_store_tokens()})


@app.route('/api/db/mercado-tokens/<int:token_id>/site-settings', methods=['GET', 'PUT'])
@internal_api_required
def api_db_mercado_token_site_settings(token_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        if request.method == "GET":
            result = bit_db_api.list_mercado_store_site_settings(token_id)
        else:
            data = request.get_json(silent=True) or {}
            result = bit_db_api.update_mercado_store_site_settings(
                token_id, data.get("settings", [])
            )
        return jsonify({"status": "success", "data": result})
    except Exception as exc:
        return _mercado_token_error_response(exc)


@app.route('/api/db/mercado-tokens/exchange', methods=['POST'])
@internal_api_required
def api_db_exchange_mercado_token():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        result = bit_db_api.exchange_mercado_store_token(
            data.get("display_name", ""), data.get("code", "")
        )
        return jsonify({"status": "success", "data": result})
    except Exception as exc:
        return _mercado_token_error_response(exc)


@app.route('/api/db/mercado-tokens/<int:token_id>/refresh', methods=['POST'])
@internal_api_required
def api_db_refresh_mercado_token(token_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        result = bit_db_api.refresh_mercado_store_token(token_id)
        return jsonify({"status": "success", "data": result})
    except Exception as exc:
        return _mercado_token_error_response(exc)


@app.route('/api/db/mercado-tokens/<int:token_id>/reputation', methods=['GET'])
@internal_api_required
def api_db_mercado_token_reputation(token_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        result = bit_db_api.get_mercado_store_reputation(token_id)
        return jsonify({"status": "success", "data": result})
    except Exception as exc:
        return _mercado_token_error_response(exc)


@app.route(
    '/api/db/mercado-communications/<int:token_id>/<action>',
    methods=['POST'],
)
@internal_api_required
def api_db_mercado_communication(token_id, action):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        result = bit_db_api.execute_mercado_store_communication(
            token_id, action, request.get_json(silent=True) or {}
        )
        return jsonify({"status": "success", "data": result})
    except Exception as exc:
        return _mercado_communication_error_response(exc)


@app.route('/api/db/mercado-tokens/<int:token_id>', methods=['PATCH', 'DELETE'])
@internal_api_required
def api_db_update_mercado_token(token_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        if request.method == "DELETE":
            affected = bit_db_api.delete_mercado_store_token(token_id)
            if not affected:
                raise KeyError("店铺授权不存在")
            return jsonify({"status": "success", "data": {"deleted": affected}})
        data = request.get_json(silent=True) or {}
        result = bit_db_api.rename_mercado_store_token(
            token_id, data.get("display_name", "")
        )
        return jsonify({"status": "success", "data": result})
    except Exception as exc:
        return _mercado_token_error_response(exc)


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


@app.route('/api/db/zying-products/product-list', methods=['POST'])
@internal_api_required
def api_db_upsert_zying_product_list():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        return jsonify({"status": "error", "message": "rows 必须是数组"}), 400
    result = db_upsert_zying_products_to_products(rows)
    return jsonify({"status": "success", "data": result})


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


@app.route('/api/db/orders', methods=['GET'])
@internal_api_required
def api_db_list_orders():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        data = db_list_orders(**_order_list_query_params(request.args))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "data": data})


@app.route('/api/db/order-sync/start', methods=['POST'])
@internal_api_required
def api_db_start_order_sync():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    token_ids = data.get("token_ids") or []
    if not isinstance(token_ids, list):
        return jsonify({"status": "error", "message": "token_ids must be an array"}), 422
    try:
        started, state = bit_order_sync.start_order_sync(
            start_date=str(data.get("start_date") or "").strip(),
            end_date=str(data.get("end_date") or "").strip(),
            token_ids=token_ids,
            mode="automatic" if str(data.get("mode")) == "automatic" else "manual",
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "data": {"started": started, "state": state}})


@app.route('/api/db/order-sync/status', methods=['GET'])
@internal_api_required
def api_db_order_sync_status():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    return jsonify({"status": "success", "data": bit_order_sync.order_sync_status()})


@app.route('/api/db/store-links', methods=['GET'])
@internal_api_required
def api_db_store_links():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        token_text = str(request.args.get("token_id") or "").strip()
        data = db_list_mercado_store_links(
            search=str(request.args.get("search") or "").strip(),
            token_id=int(token_text) if token_text else None,
            site_id=str(request.args.get("site_id") or "").strip(),
            status=str(request.args.get("status") or "").strip(),
            sales_sort=str(request.args.get("sales_sort") or "desc").strip(),
            current_only=str(request.args.get("current_only") or "1").strip().lower()
            not in ("0", "false", "no", "off"),
            page=_parse_int_param(request.args, "page", 1, 1, 1000000),
            page_size=_parse_int_param(request.args, "page_size", 1000, 10, 1000),
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "data": data})


@app.route('/api/db/store-links/bulk-update', methods=['POST'])
@internal_api_required
def api_db_bulk_update_store_links():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    link_ids = data.get("link_ids") or []
    changes = data.get("changes") or {}
    if not isinstance(link_ids, list) or not isinstance(changes, dict):
        return jsonify({"status": "error", "message": "link_ids must be an array and changes an object"}), 422
    try:
        result = db_bulk_update_mercado_store_links(link_ids, changes)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "data": result})


@app.route('/api/db/store-links/sync/start', methods=['POST'])
@internal_api_required
def api_db_start_store_link_sync():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    sync_all = data.get("sync_all") is True
    token_ids = [] if sync_all else (data.get("token_ids") or [])
    if not isinstance(token_ids, list):
        return jsonify({"status": "error", "message": "token_ids must be an array"}), 422
    try:
        started, state = bit_store_link_sync.start_store_link_sync(token_ids)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "data": {"started": started, "state": state}})


@app.route('/api/db/store-links/sync/status', methods=['GET'])
@internal_api_required
def api_db_store_link_sync_status():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    return jsonify({"status": "success", "data": bit_store_link_sync.store_link_sync_status()})


@app.route('/api/db/orders/bulk-update', methods=['POST'])
@internal_api_required
def api_db_bulk_update_orders():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    order_ids = data.get("order_ids") or []
    if not isinstance(order_ids, list):
        return jsonify({"status": "error", "message": "order_ids must be an array"}), 422
    changes = {}
    for field in (
        "workflow_status", "purchase_order", "purchase_tracking",
        "logistics_company", "purchase_cost", "purchase_remark",
    ):
        if field in data:
            changes[field] = data.get(field)
    try:
        result = bit_order_sync.bit_mysql.bulk_update_mercado_orders(
            order_ids,
            operator_id=data.get("operator_id"),
            operator_name=data.get("operator_name") or "",
            **changes,
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "data": result})


@app.route('/api/db/orders/labels', methods=['POST'])
@internal_api_required
def api_db_order_labels():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    order_ids = data.get("order_ids") or []
    if not isinstance(order_ids, list):
        return jsonify({"status": "error", "message": "order_ids must be an array"}), 422
    try:
        from bit.bit_order_labels import download_order_labels

        result = download_order_labels(order_ids)
    except (ValueError, RuntimeError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    response = send_file(
        BytesIO(result["content"]),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=result.get("filename") or "mercado-labels.pdf",
        max_age=0,
    )
    response.headers["X-Mercado-Label-Filename"] = result.get("filename") or "mercado-labels.pdf"
    response.headers["X-Mercado-Shipment-Count"] = str(result.get("shipment_count") or 0)
    successful_order_ids = [str(value) for value in result.get("order_ids") or []]
    response.headers["X-Mercado-Printed-Order-Ids"] = ",".join(successful_order_ids)
    response.headers["X-Mercado-Printed-Order-Count"] = str(len(successful_order_ids))
    response.headers["X-Mercado-Skipped-Order-Count"] = str(
        len(result.get("skipped_order_ids") or [])
    )
    response.headers["X-Mercado-Failed-Order-Count"] = str(
        len(result.get("failed_order_ids") or [])
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/db/orders/print-logs', methods=['POST'])
@internal_api_required
def api_db_order_print_logs():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    order_ids = data.get("order_ids") or []
    if not isinstance(order_ids, list):
        return jsonify({"status": "error", "message": "order_ids must be an array"}), 422
    count = bit_order_sync.bit_mysql.record_mercado_order_print_logs(
        order_ids,
        operator_id=data.get("operator_id"),
        operator_name=data.get("operator_name") or "",
    )
    return jsonify({"status": "success", "data": {"count": count}})


@app.route('/api/db/orders/<order_id>/logs', methods=['GET'])
@internal_api_required
def api_db_order_operation_logs(order_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    rows = bit_order_sync.bit_mysql.list_mercado_order_operation_logs(
        order_id,
        limit=_parse_int_param(request.args, "limit", 100, 1, 200),
    )
    return jsonify({"status": "success", "data": rows})


@app.route('/api/db/orders/<order_id>/tracking', methods=['GET'])
@internal_api_required
def api_db_order_tracking(order_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    from bit.bit_logistics import query_order_tracking

    try:
        data = query_order_tracking(order_id)
    except (KeyError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    return jsonify({"status": "success", "data": data})


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


@app.route('/api/mercado-tokens/authorization', methods=['GET'])
@login_required
def api_mercado_token_authorization():
    try:
        data = bit_db_api.get_mercado_token_authorization_info()
        return jsonify({"status": "success", "data": data})
    except Exception as exc:
        return _mercado_token_error_response(exc)


@app.route('/api/mercado-tokens', methods=['GET'])
@login_required
def api_list_mercado_tokens():
    try:
        data = bit_db_api.list_mercado_store_tokens()
        return jsonify({"status": "success", "data": data})
    except Exception as exc:
        return _mercado_token_error_response(exc)


@app.route('/api/mercado-tokens/<int:token_id>/site-settings', methods=['GET', 'PUT'])
@login_required
def api_mercado_token_site_settings(token_id):
    try:
        if request.method == "GET":
            result = bit_db_api.list_mercado_store_site_settings(token_id)
            message = ""
        else:
            data = request.get_json(silent=True) or {}
            result = bit_db_api.update_mercado_store_site_settings(
                token_id, data.get("settings", [])
            )
            message = "店铺配置已保存"
        return jsonify({"status": "success", "data": result, "message": message})
    except Exception as exc:
        return _mercado_token_error_response(exc)


@app.route('/api/mercado-tokens/exchange', methods=['POST'])
@login_required
def api_exchange_mercado_token():
    data = request.get_json(silent=True) or {}
    try:
        result = bit_db_api.exchange_mercado_store_token(
            data.get("display_name", ""), data.get("code", "")
        )
        response_data = dict(result or {})
        token_id = int(response_data.get("id") or 0)
        auto_sync = {"started": False, "queued": False}
        if token_id:
            try:
                sync_result = bit_db_api.start_store_link_sync([token_id]) or {}
                auto_sync = {
                    "started": bool(sync_result.get("started")),
                    "queued": True,
                    "task_id": str((sync_result.get("state") or {}).get("task_id") or ""),
                }
            except Exception as sync_exc:
                logging.exception("店铺授权成功，但自动拉取店铺链接启动失败")
                auto_sync["error"] = str(sync_exc)
        response_data["auto_link_sync"] = auto_sync
        message = (
            "店铺授权成功，正在自动拉取全部链接"
            if auto_sync.get("started")
            else "店铺授权成功，全部链接已加入自动拉取队列"
        )
        if auto_sync.get("error"):
            message = "店铺授权成功；自动拉取暂未启动，系统会在下次周期检查时重试"
        return jsonify({
            "status": "success",
            "data": response_data,
            "message": message,
        })
    except Exception as exc:
        return _mercado_token_error_response(exc)


@app.route('/api/mercado-tokens/<int:token_id>/refresh', methods=['POST'])
@login_required
def api_refresh_mercado_token(token_id):
    try:
        result = bit_db_api.refresh_mercado_store_token(token_id)
        return jsonify({
            "status": "success",
            "data": result,
            "message": "Token 已刷新并保存",
        })
    except Exception as exc:
        return _mercado_token_error_response(exc)


def _append_api_reputation_log(message):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {str(message or '').strip()}"
    with _api_reputation_lock:
        _api_reputation_logs.append(line)


def _api_reputation_snapshot():
    with _api_reputation_lock:
        data = dict(_api_reputation_state)
        data["rows"] = [dict(row) for row in _api_reputation_state.get("rows", [])]
        data["failures"] = [
            dict(row) for row in _api_reputation_state.get("failures", [])
        ]
        data["logs"] = list(_api_reputation_logs)
    if data.get("running"):
        data["elapsed_seconds"] = _mercado_collection_elapsed_seconds(data)
    return data


def _run_all_api_reputation_refresh():
    started_monotonic = time.monotonic()

    def update_progress(progress):
        event = str((progress or {}).get("event") or "")
        with _api_reputation_lock:
            if event == "initialized":
                total_stores = int(progress.get("total_stores") or 0)
                _api_reputation_state["total_stores"] = total_stores
                _api_reputation_state["message"] = (
                    f"正在更新 {total_stores} 家授权店铺"
                    if total_stores
                    else "没有可更新的授权店铺"
                )
            elif event == "store_success":
                rows = [dict(row) for row in (progress.get("rows") or [])]
                _api_reputation_state["completed_stores"] += 1
                _api_reputation_state["success_stores"] += 1
                _api_reputation_state["total_sites"] += len(rows)
                _api_reputation_state["rows"].extend(rows)
            elif event == "store_failure":
                _api_reputation_state["completed_stores"] += 1
                _api_reputation_state["failed_stores"] += 1
                _api_reputation_state["failures"].append({
                    "token_id": int(progress.get("token_id") or 0),
                    "store_name": str(progress.get("store_name") or ""),
                    "error": str(progress.get("error") or ""),
                })

    try:
        result = bit_reputation_info.main(
            max_workers=4,
            retry_failed=True,
            send_email=False,
            export_excel=False,
            log_callback=_append_api_reputation_log,
            progress_callback=update_progress,
        ) or {}
        finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [dict(row) for row in (result.get("api_rows") or [])]
        failures = [dict(row) for row in (result.get("failures") or [])]
        total_stores = int(result.get("total_stores") or 0)
        completed_stores = int(result.get("completed_stores") or total_stores)
        success_count = int(result.get("success_stores") or 0)
        failed_count = int(result.get("failed_stores") or len(failures))
        total_sites = int(result.get("total_sites") or len(rows))
        with _api_reputation_lock:
            _api_reputation_state.update({
                "running": False,
                "status": (
                    "success" if failed_count == 0
                    else "partial" if success_count else "error"
                ),
                "message": (
                    f"全量更新完成：成功 {success_count} 家，失败 {failed_count} 家"
                    if total_stores
                    else "没有可更新的授权店铺"
                ),
                "finished_at": finished_at,
                "elapsed_seconds": max(0, int(time.monotonic() - started_monotonic)),
                "total_stores": total_stores,
                "completed_stores": completed_stores,
                "success_stores": success_count,
                "failed_stores": failed_count,
                "total_sites": total_sites,
                "rows": rows,
                "failures": failures,
            })
    except Exception as exc:
        logging.exception("API 声誉全量更新失败")
        with _api_reputation_lock:
            _api_reputation_state.update({
                "running": False,
                "status": "error",
                "message": f"全量更新失败：{exc}",
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_seconds": max(0, int(time.monotonic() - started_monotonic)),
            })
        _append_api_reputation_log(f"任务异常终止：{exc}")


@app.route('/api/mercado-reputation/refresh', methods=['POST'])
@login_required
def api_refresh_all_mercado_reputation():
    with _api_reputation_lock:
        if _api_reputation_state.get("running"):
            data = dict(_api_reputation_state)
            data["logs"] = list(_api_reputation_logs)
            return jsonify({
                "status": "running",
                "data": data,
                "message": "API 声誉全量更新任务正在运行",
            }), 409
        _api_reputation_logs.clear()
        _api_reputation_state.update({
            "running": True,
            "status": "running",
            "message": "正在读取授权店铺",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "elapsed_seconds": 0,
            "total_stores": 0,
            "completed_stores": 0,
            "success_stores": 0,
            "failed_stores": 0,
            "total_sites": 0,
            "rows": [],
            "failures": [],
        })
    threading.Thread(
        target=_run_all_api_reputation_refresh,
        name="api-reputation-refresh",
        daemon=True,
    ).start()
    return jsonify({
        "status": "success",
        "data": _api_reputation_snapshot(),
        "message": "API 声誉全量更新已启动",
    })


@app.route('/api/mercado-reputation/status', methods=['GET'])
@login_required
def api_mercado_reputation_status():
    response = jsonify({"status": "success", "data": _api_reputation_snapshot()})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/mercado-tokens/<int:token_id>/reputation', methods=['GET'])
@login_required
def api_mercado_token_reputation(token_id):
    try:
        result = bit_db_api.get_mercado_store_reputation(token_id)
        response = jsonify({
            "status": "success",
            "data": result,
            "message": "已获取美客多官方声誉",
        })
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        return _mercado_token_error_response(exc)


MERCADO_COMMUNICATION_READ_ACTIONS = frozenset((
    "pre-sale-list",
    "pre-sale-summary",
    "pre-sale-detail",
    "post-sale-unread",
    "post-sale-messages",
    "claims-list",
    "claims-detail",
))
MERCADO_COMMUNICATION_WRITE_ACTIONS = frozenset((
    "pre-sale-answer",
    "pre-sale-delete",
    "post-sale-send",
    "claims-send",
))
MERCADO_COMMUNICATION_VIEW_POST_ACTIONS = frozenset((
    "pre-sale-translate",
))


@app.route(
    '/api/mercado-communications/<int:token_id>/<action>',
    methods=['GET', 'POST'],
)
@login_required
def api_mercado_communication(token_id, action):
    normalized_action = str(action or "").strip().lower()
    allowed = (
        normalized_action in MERCADO_COMMUNICATION_READ_ACTIONS
        if request.method == "GET"
        else normalized_action in (
            MERCADO_COMMUNICATION_WRITE_ACTIONS
            | MERCADO_COMMUNICATION_VIEW_POST_ACTIONS
        )
    )
    if not allowed:
        return jsonify({"status": "error", "message": "不支持的美客多消息操作"}), 404
    payload = request.args.to_dict() if request.method == "GET" else (request.get_json(silent=True) or {})
    try:
        result = bit_db_api.execute_mercado_store_communication(
            token_id, normalized_action, payload
        )
        response = jsonify({"status": "success", "data": result})
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        return _mercado_communication_error_response(exc)


@app.route('/api/mercado-tokens/<int:token_id>', methods=['PATCH', 'DELETE'])
@login_required
def api_update_mercado_token(token_id):
    try:
        if request.method == "DELETE":
            affected = bit_db_api.delete_mercado_store_token(token_id)
            if not affected:
                raise KeyError("店铺授权不存在")
            return jsonify({"status": "success", "data": {"deleted": affected}})
        data = request.get_json(silent=True) or {}
        result = bit_db_api.rename_mercado_store_token(
            token_id, data.get("display_name", "")
        )
        return jsonify({
            "status": "success",
            "data": result,
            "message": "店铺名称已更新",
        })
    except Exception as exc:
        return _mercado_token_error_response(exc)


@app.route("/api/yandex-console/status", methods=["GET"])
@login_required
def api_yandex_console_status():
    running = _yandex_console_health()
    return jsonify(
        {
            "status": "success",
            "data": {
                "running": running,
                "url": f"{YANDEX_CONSOLE_BASE_URL}/?embedded=1",
                "external_url": f"{YANDEX_CONSOLE_BASE_URL}/",
                "port": YANDEX_CONSOLE_PORT,
                "pid": (
                    _yandex_console_process.pid
                    if _yandex_console_process is not None
                    and _yandex_console_process.poll() is None
                    else None
                ),
            },
        }
    )


@app.route("/api/yandex-console/start", methods=["POST"])
@login_required
def api_yandex_console_start():
    running, message = ensure_yandex_console()
    return (
        jsonify(
            {
                "status": "success" if running else "error",
                "message": message,
                "data": {
                    "running": running,
                    "url": f"{YANDEX_CONSOLE_BASE_URL}/?embedded=1",
                    "external_url": f"{YANDEX_CONSOLE_BASE_URL}/",
                    "port": YANDEX_CONSOLE_PORT,
                },
            }
        ),
        200 if running else 503,
    )


@app.route("/")
@login_required
def index():
    return render_template(
        'index.html',
        current_user=session.get("workbench_user") or {},
        mercado_authorization=mercado_tokens.authorization_info(),
    )


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
    remember = str(data.get("remember", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
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
    session.permanent = remember
    session["workbench_user"] = user
    return jsonify({
        "status": "success",
        "data": session["workbench_user"],
        "remember": remember,
        "expires_in": WORKBENCH_REMEMBER_HOURS * 60 * 60 if remember else None,
    })


@app.route("/api/browser-extension/login", methods=["POST"])
def api_browser_extension_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        return jsonify({"status": "error", "message": "请输入泽顺控制台账号和密码"}), 400
    try:
        user = authenticate_workbench_user(username, password)
    except Exception as exc:
        logging.exception("泽顺商品采集助手登录失败")
        return jsonify({"status": "error", "message": f"登录接口调用失败：{exc}"}), 500
    if not user:
        return jsonify({"status": "error", "message": "账号或密码错误"}), 401
    return jsonify({
        "status": "success",
        "data": {
            "token": create_browser_extension_token(user),
            "user": user,
            "expires_in": WORKBENCH_REMEMBER_HOURS * 60 * 60,
        },
    })


@app.route("/api/browser-extension/session", methods=["GET"])
@browser_extension_login_required
def api_browser_extension_session():
    return jsonify({"status": "success", "data": {"user": g.browser_extension_user}})


@app.route("/api/browser-extension/collect", methods=["POST"])
@browser_extension_login_required
def api_browser_extension_collect():
    from erp.mercadolibre_batch_collector import validate_collection_request

    data = request.get_json(silent=True) or {}
    product = data.get("product") if isinstance(data.get("product"), dict) else data
    item_id = str(product.get("source_item_id") or "").strip().upper()
    title = str(product.get("title") or "").strip()
    source_url = str(product.get("source_url") or product.get("final_url") or "").strip()
    if not re.fullmatch(r"(?:ML[A-Z]|CBT)\d{5,}", item_id):
        return jsonify({"status": "error", "message": "未识别到有效的 Mercado Libre 商品编号"}), 400
    if not title:
        return jsonify({"status": "error", "message": "商品标题不能为空"}), 400
    try:
        source_url, _ = validate_collection_request(source_url, 1)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    product = dict(product)
    product["source_item_id"] = item_id
    product["source_url"] = source_url
    created_by = str(
        g.browser_extension_user.get("display_name")
        or g.browser_extension_user.get("username")
        or "浏览器插件"
    )
    task_id = None
    try:
        task_id = db_create_mercado_collection_task(
            source_url, 1, f"浏览器插件：{created_by}"
        )
        db_upsert_mercado_collection_items(task_id, [product])
        complete = str(product.get("scrape_status") or "partial") == "ok"
        status = "completed" if complete else "partial"
        message = (
            "浏览器插件采集完成"
            if complete
            else "浏览器插件快速采集完成，重量尺寸或详情待补充"
        )
        db_update_mercado_collection_task(
            task_id,
            status=status,
            message=message,
            collected_count=1,
            completed_count=1 if complete else 0,
            failed_count=0 if complete else 1,
            current_page=1,
            started=True,
            finished=True,
        )
    except Exception as exc:
        if task_id:
            try:
                db_update_mercado_collection_task(
                    task_id,
                    status="error",
                    message=f"浏览器插件采集失败：{exc}",
                    failed_count=1,
                    started=True,
                    finished=True,
                )
            except Exception:
                logging.exception("更新浏览器插件采集失败任务状态失败")
        logging.exception("浏览器插件采集商品入库失败")
        return jsonify({"status": "error", "message": f"商品入库失败：{exc}"}), 500
    return jsonify({
        "status": "success",
        "data": {
            "task_id": int(task_id),
            "source_item_id": item_id,
            "scrape_status": product.get("scrape_status") or "partial",
        },
    }), 201


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    if request.method == "POST" or request.path.startswith("/api/"):
        return jsonify({"status": "success"})
    return redirect(url_for("login_page"))


# 定义路由和返回内容
@app.route("/zs")
def hello_whzs():
    callback_code = request.args.get("code", "")
    if callback_code:
        try:
            code = mercado_tokens.extract_authorization_code(callback_code)
        except ValueError:
            return Response("无效的 Mercado Libre 授权码", status=400, content_type="text/plain; charset=utf-8")
        escaped_code = html.escape(code)
        response = Response(
            f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta name="referrer" content="no-referrer"><title>授权成功</title>
<style>body{{margin:0;background:#f4f7fb;color:#172033;font-family:Arial,'Microsoft YaHei',sans-serif}}main{{max-width:640px;margin:12vh auto;padding:28px;background:#fff;border:1px solid #d9e2ef;border-radius:12px;box-shadow:0 16px 40px #10284014}}h1{{font-size:22px}}code{{display:block;margin:18px 0;padding:14px;background:#f1f5f9;border-radius:8px;word-break:break-all}}button{{border:0;border-radius:8px;background:#2563eb;color:#fff;padding:10px 16px;font-weight:700;cursor:pointer}}p{{color:#667085}}</style></head>
<body><main><h1>Mercado Libre 授权成功</h1><p>复制下面的 TG Code，回到“店铺授权”模块完成添加。TG Code 只能使用一次。</p>
<code id="tg-code">{escaped_code}</code><button type="button" onclick="navigator.clipboard.writeText(document.getElementById('tg-code').textContent).then(()=>this.textContent='已复制')">复制 TG Code</button></main></body></html>""",
            content_type="text/html; charset=utf-8",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response
    if not session.get("workbench_user"):
        return redirect(url_for("login_page"))
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


def recover_interrupted_mercado_collection_tasks():
    """Mark tasks owned by an earlier workbench process as interrupted."""
    from erp.mercadolibre_collection_store import recover_interrupted_collection_tasks

    cutoff = datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    recovered = recover_interrupted_collection_tasks(cutoff=cutoff)
    if recovered:
        logging.warning("已恢复 %s 个异常中断的 Mercado 商品采集任务", recovered)
    return recovered


def start_interrupted_collection_recovery():
    """Run startup recovery without delaying the HTTP service from listening."""

    def recover_safely():
        try:
            recover_interrupted_mercado_collection_tasks()
        except Exception:
            logging.exception("恢复异常中断的 Mercado 商品采集任务失败")

    recovery_thread = threading.Thread(
        target=recover_safely,
        name="mercado-collection-startup-recovery",
        daemon=True,
    )
    recovery_thread.start()
    return recovery_thread


def start_store_link_scheduler_bootstrap():
    """Start the optional store-link scheduler without blocking Flask startup."""

    if bit_db_api.DB_MODE != "mysql":
        return None

    def start_safely():
        try:
            bit_store_link_sync.start_store_link_auto_scheduler()
            logging.info(
                "店铺链接自动同步调度已启动：每 %s 天同步一次",
                bit_store_link_sync.STORE_LINK_AUTO_SYNC_DAYS,
            )
        except Exception:
            logging.exception("启动店铺链接自动同步调度失败")

    scheduler_thread = threading.Thread(
        target=start_safely,
        name="mercado-store-link-scheduler-bootstrap",
        daemon=True,
    )
    scheduler_thread.start()
    return scheduler_thread


if __name__ == '__main__':
    start_interrupted_collection_recovery()
    start_store_link_scheduler_bootstrap()
    # 保持 5000 端口，多线程模式开启以防流式阻塞
    app.run(host='0.0.0.0', port=5000, threaded=True)
