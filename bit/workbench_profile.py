"""Self-service profile writes preserve the name used for business ownership."""
from datetime import datetime
import re


EDITABLE_FIELDS = {"personal_name": 64, "email": 128, "department": 64,
                   "wechat_id": 64, "phone": 32}
LABELS = {"personal_name": "姓名", "email": "邮箱", "department": "部门",
          "wechat_id": "微信号", "phone": "手机号"}


def public_profile(row):
    return {
        "username": row["username"],
        "salesperson": row.get("display_name") or row["username"],
        **{key: (row.get(key) or "") for key in EDITABLE_FIELDS},
        "personal_name": row.get("personal_name") or row.get("display_name") or row["username"],
    }


def profile_local(payload, *, connect, verify_password, make_password_hash,
                  validate_password, validate_email):
    if not isinstance(payload, dict):
        raise ValueError("个人资料请求格式无效")
    try:
        actor_id = int(payload.get("actor_id") or 0)
    except (ValueError, TypeError):
        raise ValueError("个人资料账号无效") from None
    if actor_id <= 0:
        raise ValueError("个人资料账号无效")
    action = payload.get("action")
    if action not in {"get", "update", "password"}:
        raise ValueError("个人资料操作无效")
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("个人资料请求格式无效")
    allowed = set()
    if action == "update":
        allowed = set(EDITABLE_FIELDS)
    elif action == "password":
        allowed = {"current_password", "new_password", "confirm_password"}
    if set(data) - set(allowed):
        raise ValueError("个人资料包含不可修改的字段")
    changes = {}
    if action == "update":
        if not data:
            raise ValueError("个人资料没有需要保存的修改")
        for field, value in data.items():
            if not isinstance(value, str):
                raise ValueError(f"{LABELS[field]}格式无效")
            value = value.strip()
            if len(value) > EDITABLE_FIELDS[field] or any(ord(char) < 32 for char in value):
                raise ValueError(f"{LABELS[field]}格式无效或过长")
            if field == "personal_name" and not value:
                raise ValueError("姓名不能为空")
            if field == "email" and value:
                value = validate_email(value)
            if field == "phone" and value and not re.fullmatch(r"\+?[0-9][0-9\s().-]{4,30}", value):
                raise ValueError("手机号格式无效")
            changes[field] = value
    if action == "password":
        current_password = data.get("current_password")
        if not isinstance(current_password, str) or not 1 <= len(current_password) <= 128:
            raise ValueError("原密码格式无效")
        if not isinstance(data.get("new_password"), str):
            raise ValueError("密码格式无效")
        new_password = validate_password(data["new_password"])
        if new_password != data.get("confirm_password"):
            raise ValueError("两次输入的新密码不一致")
        if new_password == current_password:
            raise ValueError("新密码不能与原密码相同")
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT `username`, `display_name`, `personal_name`, `email`, `department`, "
                "`wechat_id`, `phone`, `password_hash`, `is_active` FROM `workbench_users` "
                "WHERE `id` = %s" + (" FOR UPDATE" if action != "get" else ""),
                (actor_id,),
            )
            row = cursor.fetchone()
            if not row or not row.get("is_active"):
                raise ValueError("个人资料账号不可用，请重新登录")
            if action == "password":
                if not verify_password(current_password, row["password_hash"]):
                    raise ValueError("原密码不正确")
                changes = {"password_hash": make_password_hash(new_password)}
            if changes:
                if changes.get("email"):
                    cursor.execute("SELECT `id` FROM `workbench_users` WHERE `email` = %s AND `id` <> %s LIMIT 1",
                                   (changes["email"], actor_id))
                    if cursor.fetchone():
                        raise ValueError("邮箱已被其他账号使用")
                # Column names come only from the whitelist above.
                cursor.execute(
                    "UPDATE `workbench_users` SET " + ", ".join(f"`{field}` = %s" for field in changes)
                    + ", `updated_at` = %s WHERE `id` = %s",
                    (*changes.values(), datetime.now(), actor_id),
                )
                row.update(changes)
        if action != "get":
            connection.commit()
        return public_profile(row) if action != "password" else None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
