import queue
from collections import deque
import functools
import hashlib
import hmac
import os
import platform
import secrets
import subprocess
import sys
import threading
import time
import traceback
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, request, render_template, jsonify, send_file, session, redirect, url_for
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
import bit.bit_db_api as bit_db_api
import bit.bit_daily_task as bit_daily_task
import bit.bit_infractions_info as bit_infractions_info
import bit.bit_reputation_info as bit_reputation_info
from bit.bit_appeal import *
from bit.bit_runtime_lock import create_window_lease
from bit.bit_mercado_login import is_login_blocking_result
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
    db_get_latest_infraction_info = bit_db_api.get_latest_infraction_info
    db_get_latest_reputation_info = bit_db_api.get_latest_reputation_info
    db_get_ai_appeal_records = bit_db_api.get_ai_appeal_records
    db_get_window_anomalies = bit_db_api.get_window_anomalies
    db_insert_chat_info = bit_db_api.insert_chat_info
    db_insert_appeal_chat_record = bit_db_api.insert_appeal_chat_record
    db_insert_ai_appeal_record = bit_db_api.insert_ai_appeal_record
    db_insert_orders = bit_db_api.insert_orders
    db_insert_task_record = bit_db_api.insert_task_record
    db_insert_zying_product_info = bit_db_api.insert_zying_product_info
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
        get_latest_infraction_info,
        get_latest_reputation_info,
        get_ai_appeal_records,
        get_window_anomalies,
        insert_chat_info,
        insert_appeal_chat_record,
        insert_ai_appeal_record,
        insert_orders,
        insert_task_record,
        insert_zying_product_info,
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
    db_get_latest_infraction_info = get_latest_infraction_info
    db_get_latest_reputation_info = get_latest_reputation_info
    db_get_ai_appeal_records = get_ai_appeal_records
    db_get_window_anomalies = get_window_anomalies
    db_insert_chat_info = insert_chat_info
    db_insert_appeal_chat_record = insert_appeal_chat_record
    db_insert_ai_appeal_record = insert_ai_appeal_record
    db_insert_orders = insert_orders
    db_insert_task_record = insert_task_record
    db_insert_zying_product_info = insert_zying_product_info
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


def ensure_workbench_user_table():
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS `workbench_users` (
                    `id` INT NOT NULL AUTO_INCREMENT,
                    `username` VARCHAR(64) NOT NULL,
                    `password_hash` VARCHAR(255) NOT NULL,
                    `display_name` VARCHAR(64) NULL,
                    `is_active` TINYINT(1) NOT NULL DEFAULT 1,
                    `created_at` DATETIME NOT NULL,
                    `updated_at` DATETIME NOT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uniq_workbench_username` (`username`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute("SELECT COUNT(*) AS total FROM `workbench_users`")
            total = (cursor.fetchone() or {}).get("total") or 0
            if total == 0:
                username = os.environ.get("WORKBENCH_DEFAULT_USER", "admin")
                password = os.environ.get("WORKBENCH_DEFAULT_PASSWORD", "admin123456")
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    """
                    INSERT INTO `workbench_users`
                        (`username`, `password_hash`, `display_name`, `is_active`, `created_at`, `updated_at`)
                    VALUES (%s, %s, %s, 1, %s, %s)
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


def get_workbench_user(username):
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT `id`, `username`, `password_hash`, `display_name`, `is_active`
                FROM `workbench_users`
                WHERE `username` = %s
                LIMIT 1
                """,
                (username,),
            )
            return cursor.fetchone()
    finally:
        connection.close()


def build_workbench_session_user(user):
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
    }


def authenticate_workbench_user(username, password):
    if USE_DB_API:
        return bit_db_api.login_workbench_user(username, password)

    user = get_workbench_user(username)
    if not user or not user.get("is_active") or not verify_password(password, user.get("password_hash")):
        return None
    return build_workbench_session_user(user)


def login_required(view_func):
    @functools.wraps(view_func)
    def wrapper(*args, **kwargs):
        if session.get("workbench_user"):
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
}
_reputation_collect_lock = threading.Lock()
_reputation_collect_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "status": "idle",
    "message": "等待启动",
}
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
_mercado_login_log_path = CURRENT_DIR / "logs" / "bit_mercado_login_console.log"


def _read_recent_mercado_login_logs(max_bytes=256 * 1024, max_lines=800):
    try:
        with _mercado_login_log_path.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - max_bytes), os.SEEK_SET)
            content = log_file.read().decode("utf-8", errors="replace")
        return content.splitlines(keepends=True)[-max_lines:]
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
    "pid": None,
    "returncode": None,
    "log_path": str(_mercado_login_log_path),
}

APPEAL_SITES = ("墨西哥", "巴西", "哥伦比亚", "智利", "阿根廷", "乌拉圭")
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


def _append_mercado_login_task_log(text):
    text = format_log_text(text)
    if not text:
        return
    if not text.endswith("\n"):
        text += "\n"
    with _mercado_login_task_lock:
        _mercado_login_task_logs.append(text)
        _mercado_login_log_path.parent.mkdir(parents=True, exist_ok=True)
        with _mercado_login_log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(text)


def _mercado_login_task_snapshot():
    with _mercado_login_task_lock:
        return {
            **dict(_mercado_login_task_state),
            "log": "".join(_mercado_login_task_logs),
        }


def _build_mercado_login_command(shop_name="", workers=3):
    command = [
        sys.executable,
        "-u",
        "-m",
        "bit.bit_mercado_login",
    ]
    shop_name = str(shop_name or "").strip()
    if shop_name:
        command.extend(("--shop", shop_name, "--auto-login"))
    else:
        command.extend(
            (
                "--all-active-login",
                "--workers",
                str(max(1, min(int(workers or 3), 3))),
            )
        )
    command.extend(("--wait-seconds", "60", "--page-load-timeout", "20"))
    return command


def run_mercado_login_console_job(shop_name="", window_id="", workers=3):
    """后台运行登录任务，并把子进程及其工作进程输出持久化给控制台。"""
    global _mercado_login_task_process
    target = str(shop_name or "").strip() or "全部未忽略店铺"
    command = _build_mercado_login_command(shop_name=shop_name, workers=workers)
    _append_mercado_login_task_log(
        f"{get_now_time()} ===== bit_mercado_login 启动：{target} ====="
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
        )
        with _mercado_login_task_lock:
            _mercado_login_task_process = process
            _mercado_login_task_state["pid"] = process.pid
        if process.stdout is not None:
            for line in process.stdout:
                _append_mercado_login_task_log(line)
        returncode = process.wait()
        succeeded = returncode == 0
        resolve_message = ""
        if succeeded and window_id:
            try:
                db_resolve_window_anomaly(window_id)
                resolve_message = "，店铺待登录状态已自动解除"
            except Exception as exc:
                resolve_message = f"，但更新店铺状态失败：{exc}"
        message = (
            f"{target} 登录任务完成{resolve_message}"
            if succeeded
            else f"{target} 登录任务失败，退出码：{returncode}"
        )
        with _mercado_login_task_lock:
            _mercado_login_task_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "success" if succeeded else "error",
                    "message": message,
                    "returncode": returncode,
                }
            )
        _append_mercado_login_task_log(f"{get_now_time()} {message}")
    except Exception as exc:
        logging.error("bit_mercado_login console job failed: %s", exc)
        traceback.print_exc()
        with _mercado_login_task_lock:
            _mercado_login_task_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "error",
                    "message": str(exc),
                    "returncode": None,
                }
            )
        _append_mercado_login_task_log(
            f"{get_now_time()} bit_mercado_login 启动失败：{exc}"
        )
    finally:
        with _mercado_login_task_lock:
            _mercado_login_task_process = None


def start_mercado_login_console_job(shop_name="", window_id="", workers=3):
    with _mercado_login_task_lock:
        if _mercado_login_task_state.get("running"):
            return False, _mercado_login_task_snapshot()
        target = str(shop_name or "").strip() or "全部未忽略店铺"
        _mercado_login_task_state.update(
            {
                "running": True,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "status": "running",
                "message": f"{target} 登录任务已启动",
                "target": target,
                "window_id": str(window_id or "").strip(),
                "pid": None,
                "returncode": None,
            }
        )
        task_thread = threading.Thread(
            target=run_mercado_login_console_job,
            args=(shop_name, window_id, workers),
            daemon=True,
        )
        task_thread.start()
        return True, _mercado_login_task_snapshot()


def run_infraction_collect_job():
    try:
        print(f"{get_now_time()} 开始执行侵权数据采集<br>")
        target = getattr(bit_infractions_info, "main", None) or bit_infractions_info.get_infractions_info_all
        target()
        with _infraction_collect_lock:
            _infraction_collect_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "success",
                "message": "侵权数据采集完成",
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


def run_reputation_collect_job():
    try:
        print(f"{get_now_time()} 开始执行声誉数据采集<br>")
        target = getattr(bit_reputation_info, "main", None) or bit_reputation_info.get_reputation_info_all
        target()
        with _reputation_collect_lock:
            _reputation_collect_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "success",
                "message": "声誉数据采集完成",
            })
        print(f"{get_now_time()} 声誉数据采集完成<br>")
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


def build_daily_task_params(data):
    mode = str(data.get("mode", "once")).strip().lower()
    if mode not in ("once", "loop"):
        mode = "once"
    return {
        "mode": mode,
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
        "message": str(data.get("message", "") or ""),
    }


def run_daily_task_job(params, task_lock):
    try:
        print(f"{get_now_time()} 开始执行 daily_task：{params}<br>")
        if params["mode"] == "loop":
            stop_at = None
            if params["stop_after_minutes"] > 0:
                stop_at = datetime.now() + timedelta(minutes=params["stop_after_minutes"])
            bit_daily_task.loop_top_infraction_ai_appeal(
                top_n=params["top_n"],
                max_workers=params["max_workers"],
                recent_days=params["recent_days"],
                round_interval=params["round_interval"],
                site_pause=params["site_pause"],
                message=params["message"],
                only_active=params["only_active"],
                stop_at=stop_at,
                _task_lock=task_lock,
            )
            result_message = "daily_task 循环执行完成"
        else:
            bit_daily_task.run_top_infraction_ai_appeal_once(
                top_n=params["top_n"],
                max_workers=params["max_workers"],
                recent_days=params["recent_days"],
                site_pause=params["site_pause"],
                message=params["message"],
                only_active=params["only_active"],
                _task_lock=task_lock,
            )
            result_message = "daily_task 单轮执行完成"

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
    round_limit = normalize_appeal_loop_count(loop_count)
    multiple_sites_selected = len(target_sites) > 1
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
        if multiple_sites_selected:
            yield (
                f"{get_now_time()} 第 {round_number} 轮开始，将依次执行选中的 "
                f"{len(target_sites)} 个站点：{'、'.join(target_sites)}\n"
            )

        for current_site in target_sites:
            if stop_event.is_set():
                yield f"{get_now_time()} 已终结本次申诉任务，不再执行后续站点\n"
                return
            output_queue = queue.Queue()
            task_result = {"value": None}

            def run_task(run_site=current_site, run_round=round_number):
                register_thread_log_queue(output_queue)
                window_lease = None
                try:
                    print(
                        f"{get_now_time()} --- 第 {run_round} 轮任务启动："
                        f"{name} {run_site}，客服模式：{mode}"
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
                        print(f"{get_now_time()} {name} {run_site} {task_result['value']}，本轮已跳过")
                        return
                    if mode == "AI客服":
                        task_result["value"] = bit_appeal_ai.shensu(name, run_site, form, message)
                    else:
                        task_result["value"] = shensu(name, run_site, form, message, "人工客服")
                    print(f"{get_now_time()} {name} {run_site} 申诉执行完毕：{task_result['value']}")
                except Exception as e:
                    print(f"{get_now_time()} {name} {run_site} 发生错误: {str(e)}")
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
                    f"{get_now_time()} {name} {current_site} 当前操作已安全结束，"
                    "本次任务已终结\n"
                )
                return

            if is_login_blocking_result(task_result.get("value")):
                yield (
                    f"{get_now_time()} {name} {current_site} "
                    f"{task_result.get('value')}，已停止该店铺后续站点和申诉循环\n"
                )
                return

        has_next_round = (
            round_limit == PERMANENT_APPEAL_LOOP_COUNT
            or round_number < round_limit
        )
        if multiple_sites_selected:
            yield (
                f"{get_now_time()} 第 {round_number} 轮选中站点执行完成，"
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
        loop_count = normalize_appeal_loop_count(
            request.args.get("loop_count", DEFAULT_APPEAL_LOOP_COUNT)
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    form = request.args.get("form", "")
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
            "form": form,
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
                form,
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
            "message": "侵权数据采集已启动",
        })

    task_thread = threading.Thread(target=run_infraction_collect_job, daemon=True)
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
    with _reputation_collect_lock:
        if _reputation_collect_state.get("running"):
            return jsonify({
                "status": "running",
                "data": dict(_reputation_collect_state),
                "message": "声誉数据采集正在运行中"
            }), 409

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _reputation_collect_state.update({
            "running": True,
            "started_at": started_at,
            "finished_at": "",
            "status": "running",
            "message": "声誉数据采集已启动",
        })

    task_thread = threading.Thread(target=run_reputation_collect_job, daemon=True)
    task_thread.start()
    return jsonify({
        "status": "success",
        "data": dict(_reputation_collect_state),
        "message": "声誉数据采集已在后台启动"
    })


@app.route('/api/reputation/collect/status', methods=['GET'])
@login_required
def api_collect_reputation_status():
    with _reputation_collect_lock:
        return jsonify({
            "status": "success",
            "data": dict(_reputation_collect_state),
        })


@app.route('/api/tasks/daily/start', methods=['POST'])
@login_required
def api_start_daily_task():
    data = request.get_json(silent=True) or {}
    params = build_daily_task_params(data)
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


@app.route('/api/db/browser-configs', methods=['GET'])
@internal_api_required
def api_db_list_browser_configs():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
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
    db_inset_reputation_info(rows)
    return jsonify({"status": "success", "data": {"count": len(rows)}})


@app.route('/api/db/infractions/bulk', methods=['POST'])
@internal_api_required
def api_db_insert_infractions():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    db_inset_infraction_info(rows)
    return jsonify({"status": "success", "data": {"count": len(rows)}})


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
        return jsonify({
            "status": "success",
            "data": db_get_window_anomalies(active_only, limit),
        })
    except Exception as e:
        logging.error("Window anomalies query failed: %s", e)
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500


@app.route('/api/window-anomalies/<window_id>/resolve', methods=['POST'])
@login_required
def api_resolve_window_anomaly(window_id):
    try:
        affected = db_resolve_window_anomaly(window_id)
        return jsonify({"status": "success", "data": {"affected": affected}})
    except Exception as e:
        logging.error("Resolve window anomaly failed: %s", e)
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500


@app.route('/api/window-anomalies/mercado-login/status', methods=['GET'])
@login_required
def api_mercado_login_console_status():
    return jsonify({"status": "success", "data": _mercado_login_task_snapshot()})


@app.route('/api/window-anomalies/mercado-login/start', methods=['POST'])
@login_required
def api_start_mercado_login_console():
    data = request.get_json(silent=True) or {}
    window_id = str(data.get("window_id") or "").strip()
    shop_name = ""
    if window_id:
        try:
            anomaly_data = db_get_window_anomalies(active_only=True, limit=1000) or {}
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

    started, task_state = start_mercado_login_console_job(
        shop_name=shop_name,
        window_id=window_id,
        workers=_parse_int_param(data, "workers", 3, 1, 3),
    )
    if not started:
        return jsonify(
            {
                "status": "running",
                "data": task_state,
                "message": "bit_mercado_login 正在运行，请等待完成后再重新启动",
            }
        ), 409
    return jsonify(
        {
            "status": "success",
            "data": task_state,
            "message": f"{task_state['target']} 登录任务已启动",
        }
    )


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
