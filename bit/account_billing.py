"""Manual account subscriptions; money is stored as integer CNY cents."""
import calendar
import json
import logging
import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, render_template, request
from pymysql.err import ProgrammingError

PLANS = {"personal": {"name": "个人版", "cents": 29900, "sites": 10},
         "enterprise": {"name": "企业版", "cents": 59900, "sites": 30}}


def now_cn():
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)


def integer(value, label, minimum=0, maximum=10000):
    if isinstance(value, bool) or not str(value).isdigit() or not minimum <= int(value) <= maximum:
        raise ValueError(f"{label}必须为 {minimum} 至 {maximum} 的整数")
    return int(value)


def quote(plan, sites):
    if plan not in PLANS:
        raise ValueError("请选择个人版或企业版")
    sites = integer(sites, "计费站点数")
    return PLANS[plan]["cents"] + (max(0, sites - 30) * 1000 if plan == "enterprise" else 0)


def add_months(value, months):
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    month += 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def parse_date(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
        if result.tzinfo or not 2000 <= result.year <= 2100:
            raise ValueError()
        return result.replace(microsecond=0)
    except (ValueError, TypeError):
        raise ValueError("到期时间格式无效，请使用北京时间，年份 2000—2100") from None


def public_account(row, now=None):
    row = dict(row)
    now = now or now_cn()
    expires = row.get("expires_at")
    if isinstance(expires, str):
        expires = parse_date(expires)
    plan = row.get("plan") or "personal"
    sites = int(row["site_count"]) if row.get("site_count") is not None else PLANS[plan]["sites"]
    actual = int(row.get("actual_sites") or 0)
    capacity = max(PLANS[plan]["sites"], sites)
    remaining = (expires - now).total_seconds() if expires else 0
    status = "unpaid" if not expires else "expired" if remaining <= 0 else "over_limit" if actual > capacity else "active"
    row.update(plan=plan, plan_name=PLANS[plan]["name"], site_count=sites,
               actual_sites=actual, capacity=capacity, monthly_cents=quote(plan, sites),
               days_remaining=max(0, math.ceil(remaining / 86400)),
               overdue_days=max(0, math.ceil(-remaining / 86400)) if expires else 0,
               status=status, expires_at=expires.isoformat(timespec="seconds") if expires else None)
    for key, value in row.items():
        if isinstance(value, datetime):
            row[key] = value.isoformat(timespec="seconds")
    return row


def ensure_tables(cursor):
    # Subscription checks run on every protected request for non-admin users.
    # Even CREATE IF NOT EXISTS requests exclusive metadata locks in MySQL;
    # concurrent billing transactions can therefore stall login's first page.
    # Probe without reading rows; only a genuinely missing table needs DDL.
    try:
        cursor.execute("""SELECT 1 FROM workbench_subscriptions
            JOIN workbench_billing_records ON 1 = 0 LIMIT 0""")
        return
    except ProgrammingError as exc:
        if not exc.args or exc.args[0] != 1146:
            raise
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_subscriptions (
        user_id BIGINT NOT NULL PRIMARY KEY, plan VARCHAR(20) NOT NULL,
        site_count INT NOT NULL DEFAULT 0, expires_at DATETIME NULL,
        revision INT NOT NULL DEFAULT 0, updated_at DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_billing_records (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, user_id BIGINT NOT NULL,
        request_id VARCHAR(64) NOT NULL, kind VARCHAR(20) NOT NULL,
        amount_cents BIGINT NOT NULL DEFAULT 0, months INT NOT NULL DEFAULT 0,
        note VARCHAR(1000) NOT NULL, actor_id BIGINT NOT NULL, actor_name VARCHAR(100) NOT NULL,
        before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at DATETIME NOT NULL,
        UNIQUE KEY billing_request (user_id, request_id), INDEX billing_user (user_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")


ACCOUNT_SQL = """SELECT u.id AS user_id, u.username, u.display_name, u.organization_key,
    u.role_key, u.is_active, s.plan, s.site_count, s.expires_at, s.revision,
    (SELECT COUNT(*) FROM mercado_store_site_settings ss
     JOIN mercado_store_tokens t ON t.id = ss.token_id
     WHERE COALESCE(NULLIF(t.organization_key, 'default'), 'wuhan-zeshun') =
           COALESCE(NULLIF(u.organization_key, 'default'), 'wuhan-zeshun')
       AND LOWER(TRIM(ss.salesperson)) = LOWER(TRIM(COALESCE(NULLIF(u.display_name, ''), u.username)))) AS actual_sites
    FROM workbench_users u LEFT JOIN workbench_subscriptions s ON s.user_id = u.id"""


def billing_local(payload, connect=None):
    """Internal API only. Resolve the actor's privileges again at the database."""
    if connect is None:
        from bit import bit_mysql
        connect = lambda: bit_mysql.pymysql.connect(**bit_mysql.config)
    actor_id = integer(payload.get("actor_id"), "操作账号", 1, 2**63 - 1)
    action = payload.get("action")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("请求内容必须为对象")
    connection = connect()
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            # Schema DDL is completed before any locked transaction begins.
            cursor.execute("SELECT id, role_key, is_active, username, display_name FROM workbench_users WHERE id = %s", (actor_id,))
            actor = cursor.fetchone()
            if not actor or not actor["is_active"]:
                raise PermissionError("账号不存在或已停用")
            admin = actor["role_key"] == "super_admin"
            target = integer(payload.get("user_id") or actor_id, "账号", 1, 2**63 - 1)
            if (action == "list" or target != actor_id or action in {"adjust", "recharge"}) and not admin:
                raise PermissionError("只有超级管理员可以管理支付")
            if action == "list":
                cursor.execute(ACCOUNT_SQL + " WHERE u.role_key <> 'super_admin' ORDER BY u.id")
                return [public_account(row) for row in cursor.fetchall() or []]
            if action in {"get", "history"}:
                cursor.execute(ACCOUNT_SQL + " WHERE u.id = %s", (target,))
                account = cursor.fetchone()
                if not account:
                    raise ValueError("账号不存在")
                if action == "get":
                    return public_account(account)
                cursor.execute("SELECT * FROM workbench_billing_records WHERE user_id = %s ORDER BY id DESC LIMIT 500", (target,))
                records = []
                for raw in cursor.fetchall() or []:
                    row = dict(raw)
                    for key in ("before_json", "after_json"):
                        row[key.removesuffix("_json")] = json.loads(row.pop(key))
                    row["created_at"] = str(row["created_at"])
                    records.append(row)
                return records
            if action not in {"adjust", "recharge"}:
                raise ValueError("不支持的操作")
            # Lock the parent even when a subscription does not exist yet.
            cursor.execute("SELECT id FROM workbench_users WHERE id = %s FOR UPDATE", (target,))
            if not cursor.fetchone():
                raise ValueError("账号不存在")
            request_id = str(data.get("request_id") or "")
            if not 8 <= len(request_id) <= 64:
                raise ValueError("缺少有效的操作编号，请刷新页面重试")
            cursor.execute("SELECT id FROM workbench_billing_records WHERE user_id = %s AND request_id = %s FOR UPDATE", (target, request_id))
            if cursor.fetchone():
                return {"duplicate": True}
            cursor.execute(ACCOUNT_SQL + " WHERE u.id = %s FOR UPDATE", (target,))
            before = public_account(cursor.fetchone())
            if before["role_key"] == "super_admin":
                raise ValueError("超级管理员无需订阅")
            revision = integer(data.get("revision"), "版本号", 0, 2**31 - 1)
            if revision != int(before.get("revision") or 0):
                raise ValueError("账户已被其他管理员修改，请刷新后重试")
            plan = data.get("plan")
            sites = integer(data.get("site_count"), "计费站点数")
            monthly = quote(plan, sites)
            if int(before["actual_sites"]) > max(PLANS[plan]["sites"], sites):
                raise ValueError("套餐站点额度不足，请增加计费站点数或先减少站点分配")
            note = str(data.get("note") or "").strip()
            if not note or len(note) > 1000:
                raise ValueError("请填写操作备注，最多 1000 字")
            now = now_cn()
            amount, months = 0, 0
            if action == "recharge":
                months = integer(data.get("months"), "续费月数", 1, 36)
                try:
                    money = Decimal(str(data.get("amount")))
                    if not money.is_finite() or money <= 0 or money > 10000000 or money != money.quantize(Decimal("0.01")):
                        raise ValueError()
                    amount = int(money * 100)
                except (InvalidOperation, ValueError):
                    raise ValueError("实收金额必须大于 0，最多两位小数且不超过 1000 万元") from None
                expires = add_months(max(parse_date(before.get("expires_at")) or now, now), months)
            else:
                expires = parse_date(data.get("expires_at"))
            after = {"plan": plan, "site_count": sites, "expires_at": expires.isoformat() if expires else None,
                     "monthly_cents": monthly, "revision": revision + 1}
            if before.get("revision") is None:
                cursor.execute("INSERT INTO workbench_subscriptions (user_id, plan, site_count, expires_at, revision, updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
                               (target, plan, sites, expires, revision + 1, now))
            else:
                cursor.execute("UPDATE workbench_subscriptions SET plan=%s, site_count=%s, expires_at=%s, revision=%s, updated_at=%s WHERE user_id=%s",
                               (plan, sites, expires, revision + 1, now, target))
            cursor.execute("""INSERT INTO workbench_billing_records
                (user_id, request_id, kind, amount_cents, months, note, actor_id, actor_name, before_json, after_json, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (target, request_id, action, amount, months, note, actor_id,
                 actor.get("display_name") or actor["username"], json.dumps(before, ensure_ascii=False),
                 json.dumps(after, ensure_ascii=False), now))
        connection.commit()
        return after
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_blueprint(*, login_required, current_user, storage):
    bp = Blueprint("account_billing", __name__)

    @bp.route("/billing")
    @login_required
    def page():
        user = current_user() or {}
        return render_template("account_billing.html", current_user=user,
                               billing_admin=user.get("role_key") == "super_admin",
                               wallet_admin=user.get("role_key") in {"super_admin", "enterprise_admin"})

    @bp.route("/api/billing/accounts")
    @bp.route("/api/billing/me")
    @bp.route("/api/billing/accounts/<int:user_id>", methods=["GET", "PUT"])
    @bp.route("/api/billing/accounts/<int:user_id>/recharges", methods=["POST"])
    @bp.route("/api/billing/accounts/<int:user_id>/records")
    @login_required
    def api(user_id=None):
        user = current_user()
        action = "adjust" if request.method == "PUT" else "recharge" if request.method == "POST" else "history" if request.path.endswith("/records") else "list" if request.path.endswith("/accounts") else "get"
        if (action in {"list", "adjust", "recharge"} or (user_id and user_id != user.get("id"))) and user.get("role_key") != "super_admin":
            return jsonify(status="error", message="只有超级管理员可以管理支付"), 403
        try:
            data = storage({"action": action, "actor_id": user.get("id"), "user_id": user_id or user.get("id"), "data": request.get_json(silent=True) or {}})
            return jsonify(status="success", data=data)
        except PermissionError as exc:
            return jsonify(status="error", message=str(exc)), 403
        except ValueError as exc:
            return jsonify(status="error", message=str(exc)), 400
        except Exception:
            logging.exception("账户支付接口失败")
            return jsonify(status="error", message="支付数据暂时无法读取，请稍后重试"), 503

    return bp
