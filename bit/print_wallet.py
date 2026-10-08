"""Prepaid order printing, in integer CNY cents, with an append-only ledger.

Money is reserved before contacting Mercado. A PDF is released only after the
successful order subset has been settled; unavailable labels are refunded.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, redirect, request

UNIT_PRICE_CENTS = 300
MAX_ORDER_BATCH = 100
MAX_RECHARGE_CENTS = 100_000_000


class WalletInsufficientFunds(RuntimeError):
    """A whole print selection cannot be funded; no order is debited."""


class WalletConflict(RuntimeError):
    """The request has already run, or its final accounting is uncertain."""

    def __init__(self, message, *, request_status=None, request_id=None):
        super().__init__(message)
        self.request_status = request_status
        self.request_id = request_id


def now_cn():
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)


def _integer(value, label, minimum=1, maximum=2**63 - 1):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value if value is not None else "")):
        raise ValueError(f"{label}必须为整数")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{label}超出允许范围")
    return result


def _request_id(value):
    result = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", result):
        raise ValueError("缺少有效的操作编号，请刷新页面重试")
    return result


def _order_ids(values, *, allow_empty=False):
    if not isinstance(values, (list, tuple)):
        raise ValueError("订单编号必须为列表")
    result = []
    for value in values:
        if isinstance(value, (dict, list, tuple, bool)):
            raise ValueError("订单编号无效")
        order_id = str(value or "").strip()
        if not order_id or len(order_id) > 64 or any(ord(c) < 32 for c in order_id):
            raise ValueError("订单编号无效")
        if order_id not in result:
            result.append(order_id)
    if not result and not allow_empty:
        raise ValueError("请至少选择一个订单")
    if len(result) > MAX_ORDER_BATCH:
        raise ValueError(f"单次最多打印 {MAX_ORDER_BATCH} 个订单")
    return result


def _organization(value):
    result = str(value or "wuhan-zeshun").strip()
    return "wuhan-zeshun" if result in {"", "default"} else result


def _permissions(actor):
    raw = actor.get("permissions_json") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    return set(raw) if isinstance(raw, list) and all(isinstance(v, str) for v in raw) else set()


def _has_permission(actor, permission):
    permissions = _permissions(actor)
    return actor.get("role_key") == "super_admin" or "*" in permissions or permission in permissions


def _own_only(actor):
    return actor.get("role_key") != "super_admin" and (
        actor.get("role_key") == "member" or bool(actor.get("own_store_only"))
    )


def _can_list(actor):
    return not _own_only(actor) and (
        actor.get("role_key") in {"super_admin", "enterprise_admin", "warehouse", "operator"}
        or _has_permission(actor, "order_print.view")
    )


def _can_recharge(actor):
    return actor.get("role_key") in {"super_admin", "enterprise_admin"}


def ensure_tables(cursor):
    # All schema DDL completes before starting a money transaction: MySQL DDL
    # implicitly commits, including CREATE TABLE IF NOT EXISTS.
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_print_wallets (
        user_id BIGINT NOT NULL PRIMARY KEY, organization_key VARCHAR(64) NOT NULL,
        balance_cents BIGINT NOT NULL DEFAULT 0,
        total_recharge_cents BIGINT NOT NULL DEFAULT 0,
        total_charge_cents BIGINT NOT NULL DEFAULT 0, updated_at DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_print_wallet_records (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, user_id BIGINT NOT NULL,
        organization_key VARCHAR(64) NOT NULL, request_id VARCHAR(64) NOT NULL,
        kind VARCHAR(20) NOT NULL, order_id VARCHAR(64) NOT NULL DEFAULT '',
        amount_cents BIGINT NOT NULL, balance_after_cents BIGINT NOT NULL,
        actor_id BIGINT NOT NULL, actor_name VARCHAR(100) NOT NULL,
        actor_username VARCHAR(100) NOT NULL DEFAULT '',
        actor_role_key VARCHAR(64) NOT NULL DEFAULT '',
        note VARCHAR(1000) NOT NULL, created_at DATETIME NOT NULL,
        UNIQUE KEY print_wallet_event (user_id, request_id, kind, order_id),
        INDEX print_wallet_user (user_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_print_requests (
        request_id VARCHAR(64) NOT NULL PRIMARY KEY, actor_id BIGINT NOT NULL,
        order_ids_json TEXT NOT NULL, status VARCHAR(20) NOT NULL,
        reserved_cents BIGINT NOT NULL, charge_cents BIGINT NOT NULL DEFAULT 0,
        refund_cents BIGINT NOT NULL DEFAULT 0, successful_order_ids_json TEXT NULL,
        created_at DATETIME NOT NULL, settled_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_print_request_orders (
        request_id VARCHAR(64) NOT NULL, order_id VARCHAR(64) NOT NULL,
        user_id BIGINT NOT NULL, organization_key VARCHAR(64) NOT NULL,
        amount_cents BIGINT NOT NULL, token_id BIGINT NULL,
        shop_name VARCHAR(255) NOT NULL DEFAULT '', site_id VARCHAR(32) NOT NULL DEFAULT '',
        salesperson_name VARCHAR(100) NOT NULL DEFAULT '',
        salesperson_username VARCHAR(100) NOT NULL DEFAULT '', printed_at DATETIME NULL,
        print_time_source VARCHAR(20) NOT NULL DEFAULT '',
        PRIMARY KEY (request_id, order_id),
        INDEX print_request_user (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    # Additive migration only: existing audit names, timestamps and amounts are
    # preserved. In particular, a current user's name/role is not substituted
    # for a missing historical identity snapshot.
    _ensure_columns(cursor, "workbench_print_wallet_records", {
        "actor_username": "VARCHAR(100) NOT NULL DEFAULT ''",
        "actor_role_key": "VARCHAR(64) NOT NULL DEFAULT ''",
    })
    _ensure_columns(cursor, "workbench_print_request_orders", {
        "token_id": "BIGINT NULL", "shop_name": "VARCHAR(255) NOT NULL DEFAULT ''",
        "site_id": "VARCHAR(32) NOT NULL DEFAULT ''",
        "salesperson_name": "VARCHAR(100) NOT NULL DEFAULT ''",
        "salesperson_username": "VARCHAR(100) NOT NULL DEFAULT ''",
        "printed_at": "DATETIME NULL",
        "print_time_source": "VARCHAR(20) NOT NULL DEFAULT ''",
    })


def _ensure_columns(cursor, table, definitions):
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    present = {row["Field"] for row in cursor.fetchall() or []}
    for name, definition in definitions.items():
        if name in present:
            continue
        try:
            cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{name}` {definition}")
        except Exception as exc:
            # Another worker may have completed the same additive migration.
            if not (exc.args and exc.args[0] == 1060):
                raise


ACCOUNT_SQL = """SELECT u.id AS user_id, u.username, u.display_name,
    u.organization_key, u.role_key, u.is_active,
    COALESCE(w.balance_cents, 0) AS balance_cents,
    COALESCE(w.total_recharge_cents, 0) AS total_recharge_cents,
    COALESCE(w.total_charge_cents, 0) AS total_charge_cents,
    w.updated_at FROM workbench_users u
    LEFT JOIN workbench_print_wallets w ON w.user_id = u.id
      AND w.organization_key =
          COALESCE(NULLIF(NULLIF(u.organization_key, ''), 'default'), 'wuhan-zeshun')"""


def public_account(row):
    result = dict(row)
    result["user_id"] = int(result["user_id"])
    result["display_name"] = result.get("display_name") or result.get("username") or ""
    result["organization_key"] = _organization(result.get("organization_key"))
    for name in ("balance_cents", "total_recharge_cents", "total_charge_cents"):
        result[name] = int(result.get(name) or 0)
    result["printable_orders"] = max(0, result["balance_cents"] // UNIT_PRICE_CENTS)
    result["unit_price_cents"] = UNIT_PRICE_CENTS
    result["is_active"] = bool(result.get("is_active"))
    if result.get("updated_at") is not None:
        result["updated_at"] = str(result["updated_at"]).replace(" ", "T")
    return result


def _timestamp(value):
    return str(value).replace(" ", "T") if value is not None else None


def _public_record(row):
    result = dict(row)
    result["created_at"] = _timestamp(result["created_at"])
    # Keep successful IDs private: a mixed batch can include other salespeople
    # or organizations whose orders are outside the reader's permitted scope.
    successful_json = result.pop("successful_order_ids_json", None)
    request_status = result.get("request_status")
    kind, order_id = result.get("kind"), str(result.get("order_id") or "")
    is_print_event = kind in {"print", "refund"} and bool(order_id)
    requested_at = None
    if is_print_event:
        requested_at = result.get("requested_at") or result.get("print_created_at") or result.get("created_at")
    result["requested_at"] = _timestamp(requested_at)
    result["settled_at"] = _timestamp(result.get("settled_at"))
    result["print_result"] = ""
    if is_print_event:
        if request_status == "reserved":
            result["print_result"] = "pending"
        elif request_status == "cancelled":
            result["print_result"] = "cancelled"
        elif request_status == "settled" and successful_json is not None:
            try:
                successful = json.loads(successful_json)
                if not isinstance(successful, list):
                    raise ValueError()
                result["print_result"] = "completed" if order_id in successful else "refunded"
            except (ValueError, TypeError):
                result["print_result"] = "unknown"
        else:
            result["print_result"] = "refunded" if kind == "refund" else "unknown"
    printed_at = result.get("printed_at") if result["print_result"] == "completed" else None
    result["print_time_source"] = (result.get("print_time_source") or "settled") if printed_at else ""
    if result["print_result"] == "completed" and not printed_at and result.get("settled_at"):
        printed_at = result["settled_at"]
        result["print_time_source"] = "settled"
    result["printed_at"] = _timestamp(printed_at)
    for field in ("printer_id", "printer_name", "printer_username", "printer_role_key"):
        if not is_print_event:
            result[field] = None if field == "printer_id" else ""
    result.pop("print_created_at", None)
    return result


def _actor(cursor, actor_id):
    cursor.execute("""SELECT u.id, u.username, u.display_name, u.organization_key,
        u.role_key, u.warehouse_key, u.is_active, r.permissions_json, r.own_store_only,
        o.is_active AS organization_active FROM workbench_users u
        LEFT JOIN workbench_roles r ON r.role_key = u.role_key
        LEFT JOIN workbench_organizations o ON o.organization_key =
            COALESCE(NULLIF(NULLIF(u.organization_key, ''), 'default'), 'wuhan-zeshun')
        WHERE u.id = %s""", (actor_id,))
    actor = cursor.fetchone()
    if not actor or not actor.get("is_active") or not actor.get("organization_active"):
        raise PermissionError("账号或所属企业不存在、已停用")
    actor["organization_key"] = _organization(actor.get("organization_key"))
    return actor


def _scope_organization(actor, payload):
    selected = str(payload.get("organization_key") or "").strip()
    if actor["role_key"] == "super_admin":
        return _organization(selected) if selected else ""
    if selected and _organization(selected) != actor["organization_key"]:
        raise PermissionError("不能操作其他企业的充值账户")
    return actor["organization_key"]


def _account(cursor, target, actor, scope):
    cursor.execute(ACCOUNT_SQL + " WHERE u.id = %s", (target,))
    row = cursor.fetchone()
    if not row:
        raise ValueError("业务员账号不存在")
    if scope and _organization(row.get("organization_key")) != scope:
        raise PermissionError("不能操作其他企业的充值账户")
    if target != actor["id"] and not _can_list(actor):
        raise PermissionError("只能查看本人充值账户")
    return public_account(row)


def _lock_wallet(cursor, user_id, organization, now, *, require_active=True):
    # Parent rows exist even before the first recharge. Consistent numeric lock
    # order serializes recharge and print debits without wallet-insert races.
    cursor.execute("SELECT id, organization_key, is_active FROM workbench_users WHERE id = %s FOR UPDATE", (user_id,))
    user = cursor.fetchone()
    if require_active and (not user or not user.get("is_active") or _organization(user.get("organization_key")) != organization):
        raise PermissionError("业务员账号已停用或所属企业已变更")
    cursor.execute("SELECT * FROM workbench_print_wallets WHERE user_id = %s FOR UPDATE", (user_id,))
    wallet = cursor.fetchone()
    if wallet is None:
        if not require_active:
            raise WalletConflict("原预扣费钱包不存在，请管理员核对扣费记录")
        cursor.execute("""INSERT INTO workbench_print_wallets
            (user_id, organization_key, balance_cents, total_recharge_cents, total_charge_cents, updated_at)
            VALUES (%s,%s,0,0,0,%s)""", (user_id, organization, now))
        wallet = dict(user_id=user_id, organization_key=organization, balance_cents=0,
                      total_recharge_cents=0, total_charge_cents=0)
    if _organization(wallet.get("organization_key")) != organization:
        raise WalletConflict("账户企业归属已变更，请管理员核对充值余额后再打印")
    return dict(wallet)


def _save_wallet(cursor, wallet, now):
    cursor.execute("""UPDATE workbench_print_wallets SET balance_cents=%s,
        total_recharge_cents=%s, total_charge_cents=%s, updated_at=%s WHERE user_id=%s""",
        (wallet["balance_cents"], wallet["total_recharge_cents"], wallet["total_charge_cents"], now, wallet["user_id"]))


def _record(cursor, wallet, actor, request_id, kind, order_id, amount, note, now):
    cursor.execute("""INSERT INTO workbench_print_wallet_records
        (user_id, organization_key, request_id, kind, order_id, amount_cents,
         balance_after_cents, actor_id, actor_name, actor_username, actor_role_key, note, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (wallet["user_id"], wallet["organization_key"], request_id, kind, order_id, amount,
         wallet["balance_cents"], actor["id"], actor.get("display_name") or actor["username"],
         actor.get("username") or "", actor.get("role_key") or "", note, now))
    return dict(id=cursor.lastrowid, user_id=wallet["user_id"], organization_key=wallet["organization_key"],
                request_id=request_id, kind=kind, order_id=order_id, amount_cents=amount,
                balance_after_cents=wallet["balance_cents"], actor_id=actor["id"],
                actor_name=actor.get("display_name") or actor["username"],
                actor_username=actor.get("username") or "", actor_role_key=actor.get("role_key") or "",
                note=note, created_at=now.isoformat())


def _amount(data):
    if "amount_cents" in data:
        return _integer(data["amount_cents"], "充值金额（分）", maximum=MAX_RECHARGE_CENTS)
    try:
        money = Decimal(str(data.get("amount")))
        if not money.is_finite() or money <= 0 or money != money.quantize(Decimal("0.01")):
            raise ValueError()
        return _integer(int(money * 100), "充值金额", maximum=MAX_RECHARGE_CENTS)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("充值金额必须大于 0、最多两位小数，且不超过 100 万元") from None


def _resolve_orders(cursor, order_ids, actor, scope):
    placeholders = ",".join(["%s"] * len(order_ids))
    cursor.execute(f"""SELECT synced.order_id, synced.token_id, synced.site_id, synced.transfer_warehouse,
        COALESCE(NULLIF(stores.display_name, ''), NULLIF(stores.nickname, ''), synced.shop_name, '') AS shop_name,
        stores.organization_key, stores.enabled, site_settings.salesperson
        FROM mercado_synced_orders synced
        JOIN mercado_store_tokens stores ON stores.id = synced.token_id
        LEFT JOIN mercado_store_site_settings site_settings
          ON site_settings.token_id = synced.token_id AND site_settings.site_id = synced.site_id
        WHERE synced.order_id IN ({placeholders})""", order_ids)
    rows = list(cursor.fetchall() or [])
    if {str(row["order_id"]) for row in rows} != set(order_ids):
        raise PermissionError("所选订单不存在或不属于当前授权店铺")
    from bit.order_warehouses import user_warehouse
    warehouse = user_warehouse(actor)
    if warehouse is not None and any(row.get("transfer_warehouse") != warehouse for row in rows):
        raise PermissionError("只能打印绑定仓库的订单")
    organizations = {_organization(row.get("organization_key")) for row in rows}
    if scope and organizations != {scope}:
        raise PermissionError("不能打印其他企业的订单")
    if any(not row.get("enabled") for row in rows):
        raise PermissionError("所选订单所属店铺已停用")
    org_placeholders = ",".join(["%s"] * len(organizations))
    cursor.execute(f"""SELECT u.id, u.username, u.display_name, u.organization_key
        FROM workbench_users u JOIN workbench_organizations o
          ON o.organization_key = COALESCE(NULLIF(NULLIF(u.organization_key, ''), 'default'), 'wuhan-zeshun')
        WHERE u.is_active = 1 AND o.is_active = 1
          AND COALESCE(NULLIF(NULLIF(u.organization_key, ''), 'default'), 'wuhan-zeshun') IN ({org_placeholders})""",
        sorted(organizations))
    users = list(cursor.fetchall() or [])
    by_order = {}
    for row in rows:
        owner_name = str(row.get("salesperson") or "").strip().casefold()
        org = _organization(row.get("organization_key"))
        candidates = [user for user in users if _organization(user.get("organization_key")) == org
                      and owner_name and owner_name in {
                          str(user.get("username") or "").strip().casefold(),
                          str(user.get("display_name") or "").strip().casefold()}]
        if len(candidates) != 1:
            raise ValueError(f"订单 {row['order_id']} 的业务员未绑定唯一有效账号，请先完善店铺业务员配置")
        owner = candidates[0]
        if _own_only(actor) and int(owner["id"]) != actor["id"]:
            raise PermissionError("只能打印本人名下店铺订单")
        by_order[str(row["order_id"])] = dict(user_id=int(owner["id"]), organization_key=org,
            display_name=owner.get("display_name") or owner["username"], username=owner["username"],
            token_id=row["token_id"], shop_name=str(row.get("shop_name") or "")[:255],
            site_id=str(row.get("site_id") or "")[:32])
    return by_order


def _assert_automatic_orders_unprinted(cursor, order_ids, request_id):
    """READ COMMITTED queries follow owner locks to close the scan/reserve race."""
    placeholders = ",".join(["%s"] * len(order_ids))
    cursor.execute(f"""SELECT details.order_id FROM workbench_print_request_orders details
        JOIN workbench_print_requests requests ON requests.request_id = details.request_id
        WHERE details.order_id IN ({placeholders})
          AND (requests.status = 'reserved'
               OR (requests.status = 'settled' AND details.printed_at IS NOT NULL))""", order_ids)
    blocked = {str(row["order_id"]) for row in cursor.fetchall() or []}
    cursor.execute(f"""SELECT order_id FROM mercado_order_operation_logs
        WHERE order_id IN ({placeholders}) AND action_type = 'label_printed'""", order_ids)
    blocked.update(str(row["order_id"]) for row in cursor.fetchall() or [])
    if blocked:
        raise WalletConflict("订单 " + "、".join(sorted(blocked)) + " 已打印或正在打印，自动任务不会重复扣费",
                             request_id=request_id)


def _reserve(cursor, data, actor, scope, now, *, avoid_duplicate_orders=False):
    if not _has_permission(actor, "order_print.execute"):
        raise PermissionError("当前账号没有订单打印权限")
    request_id, order_ids = _request_id(data.get("request_id")), _order_ids(data.get("order_ids"))
    cursor.execute("SELECT request_id, actor_id, status FROM workbench_print_requests WHERE request_id = %s", (request_id,))
    existing = cursor.fetchone()
    if existing:
        raise _existing_request_conflict(existing, actor["id"], request_id)
    reserved = len(order_ids) * UNIT_PRICE_CENTS
    try:
        # Insert the request first: its unique key serializes same-ID calls and
        # avoids FOR UPDATE gap locks on a missing request. Every money path
        # locks its request before users; validation failures roll this back.
        cursor.execute("""INSERT INTO workbench_print_requests
            (request_id, actor_id, order_ids_json, status, reserved_cents, created_at)
            VALUES (%s,%s,%s,'reserved',%s,%s)""",
            (request_id, actor["id"], json.dumps(order_ids), reserved, now))
    except Exception as exc:
        if type(exc).__name__ == "IntegrityError" or (exc.args and exc.args[0] == 1062):
            cursor.execute("SELECT request_id, actor_id, status FROM workbench_print_requests WHERE request_id = %s FOR UPDATE", (request_id,))
            raise _existing_request_conflict(cursor.fetchone(), actor["id"], request_id) from exc
        raise
    owners = _resolve_orders(cursor, order_ids, actor, scope)
    required = Counter(owner["user_id"] for owner in owners.values())
    wallets = {}
    for user_id in sorted(required):
        owner = next(value for value in owners.values() if value["user_id"] == user_id)
        wallet = _lock_wallet(cursor, user_id, owner["organization_key"], now)
        cost = required[user_id] * UNIT_PRICE_CENTS
        if int(wallet["balance_cents"]) < cost:
            raise WalletInsufficientFunds(
                f"业务员 {owner['display_name']} 余额不足：余额 {int(wallet['balance_cents']) / 100:.2f} 元，"
                f"本次需 {cost / 100:.2f} 元，请先充值；本次未扣费")
        wallets[user_id] = wallet
    if avoid_duplicate_orders:
        # A candidate can be printed manually after the automatic scan. All
        # reservations serialize on these owner locks before creating details;
        # READ COMMITTED sees the previous reservation's committed result
        # without locking other requests ahead of their settlement refunds.
        _assert_automatic_orders_unprinted(cursor, order_ids, request_id)
    for order_id in order_ids:
        owner = owners[order_id]
        wallet = wallets[owner["user_id"]]
        wallet["balance_cents"] -= UNIT_PRICE_CENTS
        wallet["total_charge_cents"] += UNIT_PRICE_CENTS
        cursor.execute("""INSERT INTO workbench_print_request_orders
            (request_id, order_id, user_id, organization_key, amount_cents,
             token_id, shop_name, site_id, salesperson_name, salesperson_username)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (request_id, order_id, owner["user_id"], owner["organization_key"], UNIT_PRICE_CENTS,
             owner["token_id"], owner["shop_name"], owner["site_id"], owner["display_name"], owner["username"]))
        _record(cursor, wallet, actor, request_id, "print", order_id, -UNIT_PRICE_CENTS, "订单打印预扣费", now)
    for user_id in sorted(wallets):
        _save_wallet(cursor, wallets[user_id], now)
    return dict(request_id=request_id, status="reserved", reserved_cents=reserved, order_ids=order_ids)


def _existing_request_conflict(row, actor_id, request_id):
    status = row.get("status") if row and int(row["actor_id"]) == actor_id else None
    message = (
        "该打印请求已取消，预扣费用已退回；如需打印，请重新发起操作"
        if status == "cancelled" else
        "该打印请求已处理或正在处理，请查看扣费记录；确认重新打印将再次扣费"
    )
    return WalletConflict(message, request_status=status, request_id=request_id)


def _settle(cursor, data, actor, now, *, cancel=False, scope=""):
    request_id = _request_id(data.get("request_id"))
    successful = [] if cancel else _order_ids(data.get("successful_order_ids"), allow_empty=True)
    note = str(data.get("note") or "").strip()
    if cancel:
        if not _can_recharge(actor):
            raise PermissionError("只有超级管理员或企业管理员可以退回待确认打印费用")
        if not note or len(note) > 1000:
            raise ValueError("请填写取消原因，最多 1000 字")
    cursor.execute("SELECT * FROM workbench_print_requests WHERE request_id = %s FOR UPDATE", (request_id,))
    reserved = cursor.fetchone()
    if not reserved:
        raise WalletConflict("未找到打印扣费请求，请勿释放面单")
    if not cancel and int(reserved["actor_id"]) != actor["id"]:
        raise PermissionError("只能结算本人发起的打印请求")
    ordered = json.loads(reserved["order_ids_json"])
    if not set(successful).issubset(set(ordered)):
        raise WalletConflict("面单返回了未预扣费的订单，请勿释放面单")
    successful = [order_id for order_id in ordered if order_id in set(successful)]
    cursor.execute("SELECT * FROM workbench_print_request_orders WHERE request_id = %s", (request_id,))
    details = list(cursor.fetchall() or [])
    if {str(row["order_id"]) for row in details} != set(ordered):
        raise WalletConflict("打印扣费明细不完整，请管理员核对，暂不释放面单")
    if cancel and scope and any(_organization(row["organization_key"]) != scope for row in details):
        raise PermissionError("只能取消本企业的待确认打印请求")
    if reserved["status"] == "cancelled" and cancel:
        return dict(request_id=request_id, status="cancelled", charge_cents=0,
                    refund_cents=int(reserved["refund_cents"]), successful_order_ids=[], duplicate=True)
    if reserved["status"] == "settled":
        if cancel:
            raise WalletConflict("打印请求已经结算，不能取消；请核对扣费记录")
        if json.loads(reserved["successful_order_ids_json"] or "[]") != successful:
            raise WalletConflict("该打印请求已按其他结果结算，请核对扣费记录")
        return dict(request_id=request_id, status="settled", charge_cents=int(reserved["charge_cents"]),
                    refund_cents=int(reserved["refund_cents"]), successful_order_ids=successful, duplicate=True)
    if reserved["status"] != "reserved":
        if reserved["status"] == "cancelled":
            raise WalletConflict("该打印请求已取消，预扣费用已退回，暂不释放面单；如需打印，请重新发起操作",
                                 request_status="cancelled", request_id=request_id)
        raise WalletConflict("打印扣费请求状态异常，请勿释放面单", request_id=request_id)
    failed = [row for row in details if str(row["order_id"]) not in set(successful)]
    generated_at = now
    print_time_source = "settled"
    if successful and data.get("generated_at"):
        try:
            generated_at = datetime.fromisoformat(str(data["generated_at"]))
            if generated_at.tzinfo is not None:
                generated_at = generated_at.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
            requested_at = reserved["created_at"]
            if not isinstance(requested_at, datetime):
                requested_at = datetime.fromisoformat(str(requested_at))
            if not requested_at <= generated_at <= now:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("面单生成时间无效，请核对打印请求") from None
        print_time_source = "label_generated"
    wallets = {}
    for user_id in sorted({int(row["user_id"]) for row in failed}):
        row = next(value for value in failed if int(value["user_id"]) == user_id)
        wallets[user_id] = _lock_wallet(cursor, user_id, row["organization_key"], now, require_active=False)
    refund = 0
    for row in sorted(failed, key=lambda value: str(value["order_id"])):
        wallet = wallets[int(row["user_id"])]
        cents = int(row["amount_cents"])
        wallet["balance_cents"] += cents
        wallet["total_charge_cents"] -= cents
        refund += cents
        refund_note = ("管理员取消打印：" + note)[:1000] if cancel else "面单未生成，退回打印费"
        _record(cursor, wallet, actor, request_id, "refund", str(row["order_id"]), cents, refund_note, now)
    for user_id in sorted(wallets):
        _save_wallet(cursor, wallets[user_id], now)
    charge = int(reserved["reserved_cents"]) - refund
    final_status = "cancelled" if cancel else "settled"
    for order_id in successful:
        cursor.execute("""UPDATE workbench_print_request_orders SET printed_at=%s,
            print_time_source=%s WHERE request_id=%s AND order_id=%s""",
            (generated_at, print_time_source, request_id, order_id))
    cursor.execute("""UPDATE workbench_print_requests SET status=%s, charge_cents=%s,
        refund_cents=%s, successful_order_ids_json=%s, settled_at=%s WHERE request_id=%s""",
        (final_status, charge, refund, json.dumps(successful), now, request_id))
    return dict(request_id=request_id, status=final_status, charge_cents=charge, refund_cents=refund,
                successful_order_ids=successful, duplicate=False)


RECORD_FILTERS = ("order_id", "date_from", "date_to", "printer", "kind", "limit", "offset")

RECORD_SQL = """FROM workbench_print_wallet_records records
    LEFT JOIN workbench_print_requests requests
      ON requests.request_id = records.request_id AND records.kind IN ('print', 'refund')
    LEFT JOIN workbench_print_request_orders details
      ON details.request_id = records.request_id AND details.order_id = records.order_id
      AND details.user_id = records.user_id AND details.organization_key = records.organization_key
      AND records.kind IN ('print', 'refund')
    LEFT JOIN workbench_print_wallet_records print_event
      ON print_event.request_id = records.request_id AND print_event.order_id = records.order_id
      AND print_event.user_id = records.user_id AND print_event.organization_key = records.organization_key
      AND print_event.kind = 'print' AND records.kind IN ('print', 'refund')"""


def _record_filters(data, target, scope, *, history=False):
    clauses, params = ["records.user_id = %s"], [target]
    if scope:
        clauses.append("records.organization_key = %s")
        params.append(scope)
    for field, label, maximum in (("order_id", "订单编号", 64), ("printer", "打印人员", 100)):
        value = data.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or len(value.strip()) > maximum:
            raise ValueError(f"{label}筛选内容无效")
        value = value.strip()
        if not value:
            continue
        pattern = "%" + value.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        if field == "order_id":
            clauses.append("records.order_id LIKE %s ESCAPE '!'")
            params.append(pattern)
        else:
            # Refund operators can differ from the original warehouse printer.
            clauses.append("(print_event.actor_name LIKE %s ESCAPE '!' OR print_event.actor_username LIKE %s ESCAPE '!')")
            params.extend((pattern, pattern))
    kind = data.get("kind")
    if kind:
        if not isinstance(kind, str) or kind not in {"recharge", "print", "refund"}:
            raise ValueError("流水类型无效")
        clauses.append("records.kind = %s")
        params.append(kind)
    dates = {}
    for field in ("date_from", "date_to"):
        value = data.get(field)
        if not value:
            continue
        try:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError()
            dates[field] = datetime.strptime(value, "%Y-%m-%d")
            boundary = dates[field] + timedelta(days=1) if field == "date_to" else dates[field]
        except (ValueError, TypeError, OverflowError):
            raise ValueError("流水日期格式无效，请使用 YYYY-MM-DD") from None
        clauses.append("records.created_at " + ("< %s" if field == "date_to" else ">= %s"))
        params.append(boundary)
    if len(dates) == 2 and dates["date_from"] > dates["date_to"]:
        raise ValueError("开始日期不能晚于结束日期")
    limit = _integer(data.get("limit", 500 if history else 50), "每页条数", maximum=500 if history else 100)
    offset = _integer(data.get("offset", 0), "流水偏移量", minimum=0, maximum=2**31 - 1)
    return " WHERE " + " AND ".join(clauses), params, limit, offset


def _records(cursor, target, scope, data, *, history=False, warehouse=None):
    where, params, limit, offset = _record_filters(data, target, scope, history=history)
    if warehouse is not None:
        where += " AND (records.order_id = '' OR EXISTS (SELECT 1 FROM mercado_synced_orders warehouse_orders " \
                 "WHERE warehouse_orders.order_id = records.order_id AND warehouse_orders.transfer_warehouse = %s))"
        params.append(warehouse)
    cursor.execute("""SELECT records.*, requests.status AS request_status,
        requests.created_at AS requested_at, requests.settled_at,
        requests.successful_order_ids_json, details.token_id, details.shop_name, details.site_id,
        details.salesperson_name, details.salesperson_username, details.printed_at, details.print_time_source,
        print_event.actor_id AS printer_id, print_event.actor_name AS printer_name,
        print_event.actor_username AS printer_username, print_event.actor_role_key AS printer_role_key,
        print_event.created_at AS print_created_at """ + RECORD_SQL + where +
        " ORDER BY records.id DESC LIMIT %s OFFSET %s", params + [limit, offset])
    records = [_public_record(row) for row in cursor.fetchall() or []]
    if history:
        return records
    cursor.execute("SELECT COUNT(*) AS total " + RECORD_SQL + where, params)
    return dict(records=records, total=int(cursor.fetchone()["total"]), limit=limit, offset=offset)


def wallet_local(payload, connect=None):
    """Trusted internal API; actor identity and permissions are reloaded in DB."""
    if not isinstance(payload, dict):
        raise ValueError("请求内容必须为对象")
    if connect is None:
        from bit import bit_mysql
        connect = lambda: bit_mysql.pymysql.connect(**bit_mysql.config)
    actor_id = _integer(payload.get("actor_id"), "操作账号")
    action = {"accounts": "list", "me": "get"}.get(payload.get("action"), payload.get("action"))
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("请求内容必须为对象")
    connection = connect()
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            if hasattr(connection, "begin"):
                connection.begin()
            actor = _actor(cursor, actor_id)
            scope = _scope_organization(actor, payload)
            now = now_cn()
            target = _integer(payload.get("user_id") or actor_id, "业务员账号")
            if action == "reserve":
                result = _reserve(cursor, data, actor, scope, now)
            elif action == "settle":
                # Refunds remain possible after a print permission is revoked.
                result = _settle(cursor, data, actor, now)
            elif action == "cancel":
                result = _settle(cursor, data, actor, now, cancel=True, scope=scope)
            elif action == "list":
                clauses, params = [], []
                if scope:
                    clauses.append("COALESCE(NULLIF(NULLIF(u.organization_key, ''), 'default'), 'wuhan-zeshun') = %s")
                    params.append(scope)
                if not _can_list(actor):
                    clauses.append("u.id = %s")
                    params.append(actor_id)
                cursor.execute(ACCOUNT_SQL + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY u.id", params)
                result = [public_account(row) for row in cursor.fetchall() or []]
            elif action in {"get", "history", "records"}:
                account = _account(cursor, target, actor, scope)
                if action == "get":
                    result = account
                else:
                    from bit.order_warehouses import user_warehouse
                    result = _records(cursor, target, scope, data, history=action == "history",
                                      warehouse=user_warehouse(actor))
                    if action == "records":
                        result["account"] = account
            elif action == "recharge":
                if not _can_recharge(actor):
                    raise PermissionError("只有超级管理员或企业管理员可以登记充值")
                before = _account(cursor, target, actor, scope)
                request_id = _request_id(data.get("request_id"))
                amount = _amount(data)
                note = str(data.get("note") or "").strip()
                if not note or len(note) > 1000:
                    raise ValueError("请填写充值凭证或备注，最多 1000 字")
                wallet = _lock_wallet(cursor, target, before["organization_key"], now)
                cursor.execute("""SELECT * FROM workbench_print_wallet_records
                    WHERE user_id=%s AND request_id=%s AND kind='recharge' AND order_id='' FOR UPDATE""", (target, request_id))
                previous = cursor.fetchone()
                if previous:
                    if int(previous["amount_cents"]) != amount or previous["note"] != note or int(previous["actor_id"]) != actor_id:
                        raise WalletConflict("该充值操作编号已用于其他充值内容，请刷新页面")
                    result = dict(account=_account(cursor, target, actor, scope), record=_public_record(previous), duplicate=True)
                else:
                    if int(wallet["balance_cents"]) + amount > 2**63 - 1 or int(wallet["total_recharge_cents"]) + amount > 2**63 - 1:
                        raise ValueError("充值后金额超出允许范围")
                    wallet["balance_cents"] += amount
                    wallet["total_recharge_cents"] += amount
                    _save_wallet(cursor, wallet, now)
                    record = _record(cursor, wallet, actor, request_id, "recharge", "", amount, note, now)
                    result = dict(account=_account(cursor, target, actor, scope), record=record, duplicate=False)
            else:
                raise ValueError("不支持的充值账户操作")
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _automatic_wallet_local(action, data, connect=None):
    """Python-only print accounting with a fixed system audit identity.

    No request payload can select this identity. The normal owner resolution,
    active account checks, wallet locks and three-yuan charge still apply.
    """
    if action not in {"reserve", "settle"}:
        raise ValueError("自动打印只支持预扣费和结算")
    if not isinstance(data, dict):
        raise ValueError("请求内容必须为对象")
    if connect is None:
        from bit import bit_mysql
        connect = lambda: bit_mysql.pymysql.connect(**bit_mysql.config)
    actor = dict(id=0, username="system:auto-print", display_name="系统自动打印",
                 role_key="system", permissions_json=["order_print.execute"], own_store_only=False)
    connection = connect()
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            # Schema inspection can open a transaction on pooled/autocommit-off
            # connections. Finish that schema-only phase before setting the
            # characteristics of the separate accounting transaction.
            connection.commit()
            # Apply only to this transaction. The following ordinary duplicate
            # checks see commits made while waiting for owner locks, without
            # introducing wallet -> other-request lock-order deadlocks.
            cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            if hasattr(connection, "begin"):
                connection.begin()
            now = now_cn()
            result = (_reserve(cursor, data, actor, "", now, avoid_duplicate_orders=True) if action == "reserve"
                      else _settle(cursor, data, actor, now))
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _download_paid_labels(order_ids, request_id, wallet_operation, *, persist_result=None):
    """Reserve -> download -> optional durable write -> settle, or refund."""
    from bit.bit_order_labels import download_order_labels

    normalized = _order_ids(order_ids)
    request_id = _request_id(request_id)
    if persist_result is not None and not callable(persist_result):
        raise ValueError("面单持久化回调无效")
    wallet_operation("reserve", dict(order_ids=normalized, request_id=request_id))
    try:
        result = download_order_labels(normalized)
        successful = _order_ids(result.get("order_ids"), allow_empty=True)
        if not successful or not set(successful).issubset(set(normalized)):
            raise WalletConflict("面单返回的订单与扣费请求不一致，请勿释放面单")
        if not isinstance(result.get("content"), bytes) or not result["content"].startswith(b"%PDF"):
            raise ValueError("面单接口未返回有效 PDF，本次打印费用将退回")
        generated_at = now_cn().isoformat()
        if persist_result is not None:
            # Write a valid PDF before finalizing successful charges. A failed
            # write follows the same all-refund path as failed generation.
            persist_result(dict(result, order_ids=list(successful)))
    except Exception as download_error:
        try:
            wallet_operation("settle", dict(request_id=request_id, successful_order_ids=[]))
        except Exception as settle_error:
            logging.exception("打印失败后退款结果未确认 request_id=%s", request_id)
            raise WalletConflict("打印未完成，退款结果尚未确认，暂不释放面单；请管理员按操作编号核对扣费记录",
                                 request_status=getattr(settle_error, "request_status", None), request_id=request_id) from settle_error
        if isinstance(download_error, WalletConflict):
            download_error.request_status = "settled"
            download_error.request_id = request_id
        raise
    try:
        settled = wallet_operation("settle", dict(request_id=request_id,
            successful_order_ids=successful, generated_at=generated_at))
    except Exception as settle_error:
        logging.exception("面单结算结果未确认 request_id=%s", request_id)
        raise WalletConflict("打印扣费结果尚未确认，暂不释放面单；请管理员按操作编号核对扣费记录",
                             request_status=getattr(settle_error, "request_status", None), request_id=request_id) from settle_error
    result["wallet_charge_cents"] = settled["charge_cents"]
    result["wallet_request_id"] = request_id
    return result


def download_paid_labels(order_ids, actor_id, request_id, *, organization_key=""):
    """Reserve -> download -> settle. Never return a PDF with uncertain charges."""
    payload = dict(actor_id=actor_id, organization_key=organization_key)
    return _download_paid_labels(order_ids, request_id,
        lambda action, data: wallet_local(dict(payload, action=action, data=data)))


def download_automatic_paid_labels(order_ids, request_id, *, persist_result=None):
    """Internal automatic print entry; charge owners using system actor 0.

    ``persist_result`` must durably save the validated label result or raise.
    Saved files may be released only after this function returns successfully.
    """
    return _download_paid_labels(order_ids, request_id, _automatic_wallet_local,
                                 persist_result=persist_result)


def create_blueprint(*, login_required, current_user, storage):
    bp = Blueprint("print_wallet", __name__)

    @bp.route("/print-wallet")
    @login_required
    def page():
        return redirect("/billing?section=print-wallet")

    @bp.route("/api/print-wallet/accounts")
    @bp.route("/api/print-wallet/me")
    @bp.route("/api/print-wallet/accounts/<int:user_id>")
    @bp.route("/api/print-wallet/accounts/<int:user_id>/records")
    @bp.route("/api/print-wallet/accounts/<int:user_id>/recharges", methods=["POST"])
    @bp.route("/api/print-wallet/requests/<request_id>/cancel", methods=["POST"])
    @login_required
    def api(user_id=None, request_id=None):
        user = current_user() or {}
        action = "cancel" if request_id is not None else "recharge" if request.method == "POST" else "records" if request.path.endswith("/records") else "list" if request.path.endswith("/accounts") else "get"
        try:
            # User IDs and amounts may come from the browser; actor identity may not.
            body = request.get_json(silent=True) if request.method == "POST" else {}
            if body is not None and not isinstance(body, dict):
                raise ValueError("请求内容必须为对象")
            payload = dict(action=action, actor_id=user.get("id"), user_id=user_id or user.get("id"), data=body or {})
            if action == "records":
                payload["data"] = {key: request.args[key] for key in RECORD_FILTERS if key in request.args}
            if action == "cancel":
                payload["data"] = dict(body or {}, request_id=request_id)
            if user.get("role_key") == "super_admin" and request.args.get("organization_key"):
                payload["organization_key"] = request.args["organization_key"]
            data = storage(payload)
            can_recharge = user.get("role_key") in {"super_admin", "enterprise_admin"}
            if action == "list":
                data = dict(accounts=data, unit_price_cents=UNIT_PRICE_CENTS, can_recharge=can_recharge)
            elif action == "get":
                data = dict(account=data, unit_price_cents=UNIT_PRICE_CENTS, can_recharge=can_recharge)
            response = jsonify(status="success", data=data)
            response.headers["Cache-Control"] = "no-store"
            return response
        except WalletInsufficientFunds as exc:
            return jsonify(status="error", message=str(exc)), 402
        except WalletConflict as exc:
            return jsonify(status="error", message=str(exc), request_status=exc.request_status,
                           request_id=exc.request_id), 409
        except PermissionError as exc:
            return jsonify(status="error", message=str(exc)), 403
        except ValueError as exc:
            return jsonify(status="error", message=str(exc)), 400
        except Exception:
            logging.exception("读取或登记打印充值账户失败")
            return jsonify(status="error", message="打印充值数据暂时无法读取，请稍后重试"), 503

    return bp
