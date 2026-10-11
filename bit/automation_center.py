"""Enterprise automation switches, durable execution journal and server worker."""
import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, render_template, request

RULES = {"rights_reply": "权利人自动回复", "rights_cleanup": "举报链接及同名产品处理", "freight": "运费变动自动回填"}
REPLY_TEXT = "Dear brand owner, we sincerely apologize for the operational error that led to the accidental listing of your company's products. We will immediately take corrective action and promptly remove all products related to your company. Could you please withdraw your complaint?"


def connect():
    from bit import bit_mysql
    return bit_mysql.pymysql.connect(**bit_mysql.config)


def ensure_tables(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_automation_settings (
        organization_key VARCHAR(64) PRIMARY KEY,
        rights_reply TINYINT NOT NULL DEFAULT 0, rights_cleanup TINYINT NOT NULL DEFAULT 0,
        freight TINYINT NOT NULL DEFAULT 0, updated_by BIGINT NOT NULL,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_automation_runs (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, organization_key VARCHAR(64) NOT NULL,
        rule_key VARCHAR(32) NOT NULL, job_key CHAR(64) NOT NULL, subject VARCHAR(512) NOT NULL,
        status VARCHAR(24) NOT NULL, detail_json LONGTEXT NOT NULL,
        started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX automation_scope (organization_key, id),
        INDEX automation_job (organization_key, rule_key, job_key, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")


LOG_STATUSES = {"running", "success", "failed", "unknown", "skipped", "interrupted"}


def log_filter_sql(organization, payload):
    """Filter persisted history before pagination, always inside the enterprise."""
    clauses, params = ["organization_key=%s"], [organization]
    filters = payload.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("日志筛选格式无效")
    for key, column, allowed in (("type", "rule_key", set(RULES) | {"settings"}),
                                  ("status", "status", LOG_STATUSES)):
        value = str(filters.get(key) or "").strip()
        if value:
            if value not in allowed:
                raise ValueError("无效的日志类型或状态")
            clauses.append(f"{column}=%s")
            params.append(value)
    dates = {}
    for key in ("date_from", "date_to"):
        value = str(filters.get(key) or "").strip()
        if value:
            try:
                date = datetime.fromisoformat(value)
                if date.tzinfo:
                    date = date.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
                dates[key] = date
            except ValueError:
                raise ValueError("请输入有效的日志时间") from None
    if dates.get("date_from") and dates.get("date_to") and dates["date_from"] > dates["date_to"]:
        raise ValueError("开始时间不能晚于结束时间")
    if "date_from" in dates:
        clauses.append("started_at >= %s")
        params.append(dates["date_from"])
    if "date_to" in dates:
        clauses.append("started_at < %s")
        params.append(dates["date_to"] + timedelta(minutes=1))
    def pattern(value):
        return "%" + value.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
    salesperson = str(filters.get("salesperson") or "").strip()
    store = str(filters.get("store") or "").strip()
    if len(salesperson) > 128 or len(store) > 128:
        raise ValueError("业务员或店铺筛选最多 128 个字符")
    if salesperson:
        clauses.append("JSON_SEARCH(detail_json, 'one', %s, '!', '$**.salesperson') IS NOT NULL")
        params.append(pattern(salesperson))
    if store:
        # Historical names remain searchable after a store rename. Token IDs
        # also cover early reply logs that did not snapshot the store name.
        names = "JSON_SEARCH(detail_json, 'one', %s, '!', '$**.store_name', '$**.company_store', '$**.store') IS NOT NULL"
        token_match = " OR ".join(
            f"JSON_CONTAINS(JSON_EXTRACT(detail_json, '$**.{key}'), CAST(t.id AS JSON))"
            for key in ("token_id", "source_token_id"))
        clauses.append("(" + names + " OR EXISTS (SELECT 1 FROM mercado_store_tokens t WHERE "
                       "COALESCE(NULLIF(NULLIF(t.organization_key, 'default'), ''), 'wuhan-zeshun')=%s "
                       "AND (t.display_name LIKE %s ESCAPE '!' OR CAST(t.id AS CHAR)=%s) AND (" + token_match + ")))" )
        params.extend((pattern(store), organization, pattern(store), store))
    before = max(0, int(payload.get("before") or 0))
    if before:
        clauses.append("id < %s")
        params.append(before)
    return " AND ".join(clauses), tuple(params)


def local(payload):
    from bit.knowledge_organization import normalize
    organization = normalize(payload.get("organization_key"))
    operation = payload.get("action", "list")
    where, parameters = log_filter_sql(organization, payload)
    connection = connect()
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            if operation == "save":
                data = payload.get("data")
                if not isinstance(data, dict) or not data or set(data) - RULES.keys() or any(type(v) is not bool for v in data.values()):
                    raise ValueError("开关必须为布尔值")
                cursor.execute("INSERT IGNORE INTO workbench_automation_settings (organization_key,updated_by) VALUES (%s,%s)",
                               (organization, int(payload["actor_id"])))
                fields = list(data)
                cursor.execute("UPDATE workbench_automation_settings SET " + ",".join(f"`{k}`=%s" for k in fields)
                               + ",updated_by=%s WHERE organization_key=%s",
                               tuple(int(data[k]) for k in fields) + (int(payload["actor_id"]), organization))
                cursor.execute("INSERT INTO workbench_automation_runs (organization_key,rule_key,job_key,subject,status,detail_json) VALUES (%s,'settings',%s,'修改自动执行开关','success',%s)",
                               (organization, uuid.uuid4().hex, json.dumps({"actor_id": payload["actor_id"], "switches": data}, ensure_ascii=False)))
            elif operation != "list":
                raise ValueError("无效自动化操作")
            cursor.execute("SELECT * FROM workbench_automation_settings WHERE organization_key=%s", (organization,))
            settings = cursor.fetchone() or {}
            cursor.execute("SELECT * FROM workbench_automation_runs WHERE " + where + " ORDER BY id DESC LIMIT 51", parameters)
            rows = list(cursor.fetchall() or [])
            more = len(rows) > 50
            rows = rows[:50]
            for row in rows:
                row["detail"] = json.loads(row.pop("detail_json"))
                for field in ("started_at", "updated_at"):
                    row[field] = str(row[field])
        connection.commit()
        return {"settings": {k: bool(settings.get(k)) for k in RULES}, "runs": rows,
                "next_before": rows[-1]["id"] if more else None, "reply_text": REPLY_TEXT}
    finally:
        connection.close()


def enabled(organization, rule):
    from bit.knowledge_organization import normalize
    organization = normalize(organization)
    if rule not in RULES:
        return False
    connection = connect()
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            cursor.execute(f"SELECT `{rule}` AS enabled FROM workbench_automation_settings WHERE organization_key=%s", (organization,))
            return bool((cursor.fetchone() or {}).get("enabled"))
    finally:
        connection.close()


def begin(organization, rule, identity, subject):
    """Called under the cross-process/cross-host MySQL worker lease."""
    key = hashlib.sha256(str(identity).encode()).hexdigest()
    connection = connect()
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            cursor.execute("SELECT id,status, TIMESTAMPDIFF(SECOND, updated_at, CURRENT_TIMESTAMP) AS age FROM workbench_automation_runs WHERE organization_key=%s AND rule_key=%s AND job_key=%s ORDER BY id DESC LIMIT 1", (organization, rule, key))
            previous = cursor.fetchone()
            if previous and (previous["status"] in {"success", "unknown"} or int(previous["age"]) < 3600):
                return None
            if previous and previous["status"] == "running":
                cursor.execute("UPDATE workbench_automation_runs SET status='interrupted' WHERE id=%s", (previous["id"],))
            cursor.execute("INSERT INTO workbench_automation_runs (organization_key,rule_key,job_key,subject,status,detail_json) VALUES (%s,%s,%s,%s,'running','[]')", (organization, rule, key, str(subject)[:512]))
            run_id = cursor.lastrowid
        connection.commit()
        return run_id
    finally:
        connection.close()


def journal(run_id, detail, status=None):
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT detail_json FROM workbench_automation_runs WHERE id=%s FOR UPDATE", (run_id,))
            row = cursor.fetchone()
            entries = json.loads(row["detail_json"])
            entries.append({"time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), **detail})
            cursor.execute("UPDATE workbench_automation_runs SET detail_json=%s, status=COALESCE(%s,status) WHERE id=%s",
                           (json.dumps(entries, ensure_ascii=False, default=str), status, run_id))
        connection.commit()
    finally:
        connection.close()


def due_for_reply(value, now=None):
    try:
        due = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        seconds = (due - (now or datetime.now(timezone.utc))).total_seconds()
        return 0 < seconds <= 24 * 3600
    except (ValueError, TypeError):
        return False


def execute_job(organization, rule, identity, subject, operation):
    if not enabled(organization, rule):
        return
    run_id = begin(organization, rule, identity, subject)
    if not run_id:
        return
    try:
        result = operation(run_id)
        journal(run_id, {"result": result}, result.get("automation_status", "success"))
    except Exception as exc:
        logging.exception("自动化执行失败：%s", subject)
        journal(run_id, {"error": str(exc)}, "failed")


def reply(token, row, run_id):
    from bit import mercado_rights_reply as replies
    journal(run_id, {"store_name": token.get("display_name") or row.get("store_name"), "salesperson": row.get("salesperson"), "token_id": token["id"], "case_id": row["infraction_id"], "item_id": row["item_id"], "due_at": row.get("due_at"), "text": REPLY_TEXT})
    # Recheck the authoritative deadline immediately before submission.
    client, _ = replies.store_client(token["id"])
    try:
        detail = replies.read_case(client, row["infraction_id"], row["item_id"])
        if not replies.can_reply(detail) or not due_for_reply(detail.get("due_date")):
            return {"automation_status": "skipped", "message": "平台状态或截止时间已变化，本次未发送"}
    finally:
        client.session.close()
    require_enabled(token.get("organization_key") or "wuhan-zeshun", "rights_reply")
    result = replies.reply_case(token["id"], row, REPLY_TEXT)
    result["automation_status"] = {"submitted": "success", "unknown": "unknown", "closed": "skipped"}.get(result.get("mode"), "failed")
    return result


def cleanup(token, rows, run_id):
    from bit.rights_holder_policy import process_rights_holder_product
    journal(run_id, {"token_id": token["id"], "store_name": token.get("display_name"), "reports": rows})
    title = str(rows[0].get("title") or "").strip()
    if not title:
        from bit.mercado_rights_reply import store_client
        client, _ = store_client(token["id"])
        try:
            item = client.get_marketplace_item(rows[0]["item_id"])
            if str(item.get("id")) != str(rows[0]["item_id"]):
                raise ValueError("举报商品编号核验失败")
            title = str(item.get("title") or "").strip()
            if not title:
                raise ValueError("平台未返回举报商品标题")
        finally:
            client.session.close()
    result = process_rights_holder_product(token, title, rows, max_sold_quantity=None,
        before_write=lambda: require_enabled(token.get("organization_key") or "wuhan-zeshun", "rights_cleanup"),
        on_result=lambda detail: journal(run_id, detail))
    result["automation_status"] = "success" if result["status"] == "success" else "failed"
    return result


def freight(organization, row, token_ids, run_id):
    from bit import weight_dimensions_records as weights
    from erp.package_update_weight import package_update_weight_g
    row = dict(row)
    row["_automation_run_id"] = run_id
    row["_automation_organization"] = organization
    row["_automation_actual_weight_only"] = True
    # Each change fingerprint owns its own run, independent of manual execution status.
    row.pop("_zeshun_status", None)
    weight = package_update_weight_g(row)
    journal(run_id, {"token_id": row.get("_token_id"), "store_name": row.get("company_store"), "salesperson": row.get("salesperson"), "order_number": row.get("order_number"), "actual_weight_g": row.get("actual_weight_g"),
                     "dimensions_cm": row.get("actual_dimensions_cm"), "submitted_weight_g": weight})
    task_id, owner = uuid.uuid4().hex, "automation:" + organization
    with weights._lock:
        weights._tasks[task_id] = {"owner": owner, "records": [row], "authorized_ids": token_ids,
                                   "status": "ready", "execute_status": "queued"}
    try:
        weights._run_execute(task_id, owner, "zeshun")
        return {"order_number": row.get("order_number"), "automation_status": "success" if row.get("_zeshun_status") == "成功" else "failed",
                "message": row.get("execution_error") or row.get("execution_status")}
    finally:
        with weights._lock:
            weights._tasks.pop(task_id, None)


def run_cycle():
    from bit import bit_db_api
    from bit.knowledge_organization import normalize
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK('workbench-automation-center',0) AS acquired")
            if not cursor.fetchone()["acquired"]:
                return
            ensure_tables(cursor)
            cursor.execute("SELECT * FROM workbench_automation_settings WHERE rights_reply=1 OR rights_cleanup=1 OR freight=1")
            settings = list(cursor.fetchall() or [])
        # Keep the advisory worker lease, but release the read transaction before
        # invoking jobs that open their own connections and write these tables.
        connection.commit()
        if not settings:
            return
        tokens = bit_db_api.list_mercado_store_tokens().get("rows", [])
        for setting in settings:
            organization = setting["organization_key"]
            scoped = [t for t in tokens if normalize(t.get("organization_key") or "wuhan-zeshun") == organization]
            ids = [int(t["id"]) for t in scoped]
            if not ids:
                continue
            for rule in RULES:
                if not setting[rule]:
                    continue
                try:
                    scan_rule(rule, connection, organization, scoped, ids)
                except Exception as exc:
                    logging.exception("自动规则扫描失败：%s / %s", organization, rule)
                    run_id = begin(organization, rule, "scan:" + datetime.now(timezone.utc).strftime("%Y-%m-%d-%H"), "读取待执行数据失败")
                    if run_id:
                        journal(run_id, {"error": str(exc)}, "failed")

    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK('workbench-automation-center')")
        finally:
            connection.close()


def start_scheduler():
    from bit import bit_db_api
    if bit_db_api.DB_MODE != "mysql":
        return
    def loop():
        while True:
            try:
                run_cycle()
            except Exception:
                logging.exception("自动化执行中心扫描失败")
            threading.Event().wait(60)
    thread = threading.Thread(target=loop, name="automation-center", daemon=True)
    thread.start()
    return thread


def create_blueprint(*, login_required, current_user, has_permission, selected_organization):
    blueprint = Blueprint("automation_center", __name__)

    def authorized():
        user = current_user() or {}
        return user.get("role_key") in {"super_admin", "enterprise_admin"} and not user.get("own_store_only") and has_permission(user, "tasks.execute")

    @blueprint.route("/automation-center")
    @login_required
    def page():
        if not authorized():
            return "只有企业管理员可以管理企业自动执行规则", 403
        return render_template("automation_center.html", rules=RULES, reply_text=REPLY_TEXT, embedded=request.args.get("embedded") == "1")

    @blueprint.route("/api/automation-center", methods=["GET", "PATCH"])
    @login_required
    def api():
        if not authorized():
            return jsonify(status="error", message="无自动化管理权限"), 403
        from bit import bit_db_api
        user = current_user()
        organization = selected_organization(user) or user.get("organization_key")
        if not organization:
            return jsonify(status="error", message="请先选择企业"), 400
        try:
            result = bit_db_api._mercado_action_center_call("automation_center", {
                "organization_key": organization, "actor_id": user["id"],
                "action": "save" if request.method == "PATCH" else "list",
                "data": request.get_json(silent=True), "before": request.args.get("before", 0),
                "filters": {key: request.args.get(key, "") for key in ("salesperson", "store", "type", "date_from", "date_to", "status")},
            })
            response = jsonify(status="success", data=result)
            response.headers["Cache-Control"] = "no-store"
            return response
        except (ValueError, TypeError) as exc:
            return jsonify(status="error", message=str(exc)), 400
    return blueprint


def require_enabled(organization, rule):
    if not enabled(organization, rule):
        raise ValueError("自动执行开关已关闭，停止后续操作")


def backfill_local_package(row, weight):
    """Update exact-title products and the verified link inside one transaction."""
    from decimal import Decimal
    import re
    from erp import mercadolibre_collection_store as products, mercadolibre_store_link_store as links
    organization = row["_automation_organization"]
    require_enabled(organization, "freight")
    title = str((row.get("_history_context") or {}).get("link_name") or "").strip()
    if not title:
        raise ValueError("缺少已核实的同名标题，无法回填泽顺产品")
    fields = {"weight_g": weight}
    dimensions = str(row.get("actual_dimensions_cm") or "").strip()
    if dimensions:
        values = [Decimal(v.strip()) for v in re.split(r"[x×*]", dimensions.lower())]
        if len(values) != 3 or any(not v.is_finite() or v <= 0 for v in values):
            raise ValueError("实际尺寸无效")
        fields.update(zip(("package_length_cm", "package_width_cm", "package_height_cm"), values))
        fields["volumetric_weight_kg"] = values[0] * values[1] * values[2] / 6000
    connection = connect()
    from erp.rights_holder_policy import lock_matching_products
    from erp.short_transaction import run as short_transaction
    def update():
        with connection.cursor() as cursor:
            before = lock_matching_products(cursor, organization, title,
                "id,title,weight_g,package_length_cm,package_width_cm,package_height_cm")
            assignment = ",".join(f"`{field}`=%s" for field in fields)
            if before:
                cursor.execute(f"UPDATE `{products.PRODUCT_TABLE}` SET {assignment}, weight_basis='mercado_remote_update', profitability_updated_at=NULL WHERE organization_key=%s AND id IN ({','.join(['%s']*len(before))})", tuple(fields.values()) + (organization,) + tuple(r["id"] for r in before))
            cursor.execute(f"UPDATE `{links.STORE_LINK_TABLE}` SET {assignment} WHERE token_id=%s AND item_id=%s",
                           tuple(fields.values()) + (int(row["_token_id"]), row["marketplace_item_id"]))
        return {"title": title, "before": before, "after": fields, "product_count": len(before),
                "item_id": row["marketplace_item_id"]}
    try:
        with connection.cursor() as cursor:
            products.ensure_collection_tables(cursor)
            links.ensure_store_link_table(cursor)
        connection.commit()
        return short_transaction(connection, update)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def latest_freight_records(records, token_ids):
    """Older orders must never overwrite a newer measurement of the same title."""
    latest = {}
    for row in records:
        if int(row.get("_token_id") or 0) not in token_ids or not row.get("_dimension_compensation_confirmed"):
            continue
        key = str(row.get("title") or "").strip() or (row.get("_token_id"), row.get("marketplace_item_id"))
        rank = (str(row.get("time") or ""), str(row.get("order_number") or ""))
        previous = latest.get(key)
        if previous is None or rank > (str(previous.get("time") or ""), str(previous.get("order_number") or "")):
            latest[key] = row
    return list(latest.values())


def scan_rule(rule, connection, organization, scoped, ids):
    from bit import bit_db_api
    if rule == "rights_reply":
        from bit.mercado_action_center import _rights_holder_replies
        token_map = {int(t["id"]): t for t in scoped}
        for row in _rights_holder_replies(bit_db_api, ids, include_replied=True):
            if due_for_reply(row.get("due_at")):
                execute_job(organization, "rights_reply", f"{row['token_id']}:{row['infraction_id']}", row.get("title") or row["item_id"],
                            lambda run, r=row: reply(token_map[int(r["token_id"])], r, run))
    if rule == "rights_cleanup":
        for token in scoped:
            with connection.cursor() as cursor:
                cursor.execute("SELECT source_id,item_id,title,rights_holder,reason,salesperson,site_id FROM erp_mercadolibre_infractions WHERE token_id=%s AND source_type='rights_holder' AND (reason_code IS NULL OR reason_code <> 'LEGACY_PAGE') AND resolution_status <> 'appeal_success' AND (is_current=1 OR resolution_status='appeal_failed')", (token["id"],))
                reports = list(cursor.fetchall() or [])
            connection.commit()
            groups = {}
            for row in reports:
                groups.setdefault(str(row.get("title") or "").strip() or row["item_id"], []).append(row)
            for title, rows in groups.items():
                from erp.rights_holder_policy import same_title_links
                targets = same_title_links(title, organization)
                target_key = sorted((int(link["token_id"]), str(link["item_id"])) for link in targets
                                    if link.get("is_current", 1) and link.get("status") != "deleted")
                identity = json.dumps([token["id"], title, sorted(str(r["source_id"]) for r in rows), target_key])
                execute_job(organization, "rights_cleanup", identity, title,
                            lambda run, t=token, r=rows: cleanup(t, r, run))
    if rule == "freight":
        page, candidates = 1, []
        while True:
            data = bit_db_api.list_weight_dimensions_records(filters={"store_ids": ids}, page=page, page_size=100)
            records = data.get("records", [])
            candidates.extend(records)
            if not records or page * 100 >= int(data.get("record_total") or 0):
                break
            page += 1
        for row in latest_freight_records(candidates, ids):
            identity = json.dumps([row.get(k) for k in ("order_number", "actual_weight_g", "actual_dimensions_cm", "marketplace_item_id")], default=str)
            execute_job(organization, "freight", identity, row.get("title") or row.get("order_number"),
                        lambda run, r=row: freight(organization, r, ids, run))
