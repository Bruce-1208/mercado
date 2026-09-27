"""Enterprise-scoped employee assignments, shared by server and API clients."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, render_template, request


MANAGER_ROLES = {"enterprise_admin", "super_admin"}


def task_fields(data):
    result = {}
    for name, label, limit in (("title", "标题", 200), ("content", "内容", 10000)):
        value = data.get(name)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValueError(f"请填写{label}，最多 {limit} 字")
        result[name] = value.strip()
    for name in ("starts_at", "due_at"):
        try:
            value = datetime.fromisoformat(str(data.get(name) or ""))
            if value.tzinfo is not None:
                raise ValueError()
            result[name] = value.replace(second=0, microsecond=0)
        except ValueError:
            raise ValueError("请填写有效的开始时间和截止时间（北京时间）") from None
    if result["due_at"] <= result["starts_at"]:
        raise ValueError("截止时间必须晚于开始时间")
    assignee = data.get("assignee_id")
    if isinstance(assignee, bool) or not str(assignee).isdigit() or int(assignee) <= 0:
        raise ValueError("请选择完成任务的业务员")
    result["assignee_id"] = int(assignee)
    return result


def ensure_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workbench_employee_tasks (
            id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            organization_key VARCHAR(64) NOT NULL,
            title VARCHAR(200) NOT NULL, content TEXT NOT NULL,
            starts_at DATETIME NOT NULL, due_at DATETIME NOT NULL,
            assignee_id BIGINT NOT NULL, created_by BIGINT NOT NULL,
            is_completed TINYINT(1) NOT NULL DEFAULT 0,
            completion_note TEXT NOT NULL, completed_at DATETIME NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            INDEX idx_employee_tasks_scope (organization_key, assignee_id, starts_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def employee_tasks_local(payload, connect=None):
    """Called only by the authenticated internal DB API with a server-built scope."""
    if connect is None:
        from bit import bit_mysql
        connect = lambda: bit_mysql.pymysql.connect(**bit_mysql.config)
    organization = str(payload.get("organization_key") or "")
    actor_id = int(payload.get("actor_id") or 0)
    if not organization or not actor_id:
        raise ValueError("缺少企业或当前账号信息")
    manager = bool(payload.get("manager"))
    operation = payload.get("action")
    data = payload.get("data") or {}
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)
    connection = connect()
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            if operation == "list":
                where = "t.organization_key = %s"
                params = [organization]
                if not manager or payload.get("mine"):
                    where += " AND t.assignee_id = %s"
                    params.append(actor_id)
                cursor.execute(f"""SELECT t.*, COALESCE(NULLIF(u.display_name, ''), u.username) AS assignee_name
                    FROM workbench_employee_tasks t
                    LEFT JOIN workbench_users u ON u.id = t.assignee_id AND u.organization_key = t.organization_key
                    WHERE {where} ORDER BY t.is_completed, t.due_at, t.id DESC""", tuple(params))
                rows = []
                for raw in cursor.fetchall() or []:
                    row = dict(raw)
                    row["is_completed"] = bool(row["is_completed"])
                    row["overdue"] = not row["is_completed"] and row["due_at"] < now
                    row["not_started"] = row["starts_at"] > now
                    for key, value in row.items():
                        if isinstance(value, datetime):
                            row[key] = value.isoformat() + "+08:00"
                    rows.append(row)
                users = []
                if manager:
                    cursor.execute("""SELECT id, username, display_name FROM workbench_users
                        WHERE organization_key = %s AND is_active = 1 ORDER BY display_name, id""", (organization,))
                    users = list(cursor.fetchall() or [])
                return {"rows": rows, "assignees": users, "can_manage": manager, "actor_id": actor_id}
            if operation in {"create", "edit"}:
                if not manager:
                    raise ValueError("仅企业管理员可以设置员工任务")
                fields = task_fields(data)
                cursor.execute("""SELECT id FROM workbench_users
                    WHERE id = %s AND organization_key = %s AND is_active = 1""",
                    (fields["assignee_id"], organization))
                if not cursor.fetchone():
                    raise ValueError("请选择本企业的有效业务员账号")
                values = tuple(fields[key] for key in ("title", "content", "starts_at", "due_at", "assignee_id"))
                if operation == "create":
                    cursor.execute("""INSERT INTO workbench_employee_tasks
                        (title, content, starts_at, due_at, assignee_id, organization_key, created_by,
                         completion_note, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, '', %s, %s)""",
                        values + (organization, actor_id, now, now))
                    task_id = cursor.lastrowid
                else:
                    task_id = int(payload.get("task_id") or 0)
                    cursor.execute("SELECT * FROM workbench_employee_tasks WHERE id = %s AND organization_key = %s FOR UPDATE", (task_id, organization))
                    existing = cursor.fetchone()
                    if not existing:
                        raise ValueError("任务不存在或无权访问")
                    # Reassignment starts a fresh completion state for the new owner.
                    reassigned = int(existing["assignee_id"]) != fields["assignee_id"]
                    cursor.execute("""UPDATE workbench_employee_tasks SET title=%s, content=%s,
                        starts_at=%s, due_at=%s, assignee_id=%s, updated_at=%s,
                        is_completed=%s, completion_note=%s, completed_at=%s
                        WHERE id=%s AND organization_key=%s""", values + (now,
                        0 if reassigned else existing["is_completed"],
                        "" if reassigned else existing["completion_note"],
                        None if reassigned else existing["completed_at"], task_id, organization))
            elif operation == "feedback":
                if type(data.get("is_completed")) is not bool:
                    raise ValueError("请选择是否完成")
                note = data.get("completion_note", "")
                if not isinstance(note, str) or len(note) > 2000:
                    raise ValueError("完成备注最多 2000 字")
                task_id = int(payload.get("task_id") or 0)
                cursor.execute("""SELECT * FROM workbench_employee_tasks
                    WHERE id=%s AND organization_key=%s AND assignee_id=%s FOR UPDATE""",
                    (task_id, organization, actor_id))
                existing = cursor.fetchone()
                if not existing:
                    raise ValueError("任务不存在或不是分配给您的任务")
                if existing["starts_at"] > now:
                    raise ValueError("任务尚未开始")
                completed_at = (existing["completed_at"] or now) if data["is_completed"] else None
                cursor.execute("""UPDATE workbench_employee_tasks SET is_completed=%s,
                    completion_note=%s, completed_at=%s, updated_at=%s
                    WHERE id=%s AND organization_key=%s AND assignee_id=%s""",
                    (data["is_completed"], note.strip(), completed_at, now, task_id, organization, actor_id))
            else:
                raise ValueError("员工任务操作无效")
        connection.commit()
        return {"id": task_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_employee_tasks_blueprint(*, login_required, current_user, has_permission, storage, selected_organization):
    bp = Blueprint("employee_tasks", __name__)

    def context():
        user = current_user() or {}
        manager = user.get("role_key") in MANAGER_ROLES
        organization = user.get("organization_key") or "wuhan-zeshun"
        if user.get("role_key") == "super_admin":
            organization = selected_organization(user) or organization
        return user, manager, organization

    @bp.route("/employee-tasks")
    @login_required
    def page():
        user, manager, organization = context()
        if not manager:
            return "仅企业管理员可以管理员工任务", 403
        return render_template("employee_tasks.html", current_user=user, organization_key=organization)

    @bp.route("/api/employee-tasks", methods=["GET", "POST"])
    @bp.route("/api/employee-tasks/<int:task_id>", methods=["PUT", "PATCH"])
    @login_required
    def api(task_id=None):
        user, manager, organization = context()
        if not manager and not has_permission(user, "tasks.view"):
            return jsonify(status="error", message="无任务查看权限"), 403
        action = {"GET": "list", "POST": "create", "PUT": "edit", "PATCH": "feedback"}[request.method]
        if action in {"create", "edit"} and not manager:
            return jsonify(status="error", message="仅企业管理员可以设置员工任务"), 403
        data = request.get_json(silent=True) if request.method != "GET" else {}
        if not isinstance(data, dict):
            return jsonify(status="error", message="请求格式无效"), 400
        try:
            result = storage({"action": action, "organization_key": organization,
                "actor_id": user.get("id"), "manager": manager, "task_id": task_id,
                "mine": request.args.get("mine") == "1", "data": data})
            return jsonify(status="success", data=result)
        except ValueError as exc:
            return jsonify(status="error", message=str(exc)), 400
        except Exception:
            logging.exception("员工任务操作失败")
            return jsonify(status="error", message="员工任务暂时无法保存或读取，请稍后重试"), 500

    return bp
