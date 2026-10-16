"""Enterprise automation switches, durable execution journal and server worker."""
import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, render_template, request

RULES = {"rights_reply": "权利人自动回复", "rights_cleanup": "举报链接及同名产品处理", "freight": "运费变动自动回填"}
SCHEDULED_RULES = tuple(RULES)
RULES["publish_infringement"] = "上架时自动侵权拦截"
REPLY_TEXT = "Dear brand owner, we sincerely apologize for the operational error that led to the accidental listing of your company's products. We will immediately take corrective action and promptly remove all products related to your company. Could you please withdraw your complaint?"
_schema_guard = threading.Lock()
_status_index_ready = False
_publish_setting_ready = False


def connect():
    from bit import bit_mysql
    return bit_mysql.pymysql.connect(**bit_mysql.config)


def ensure_tables(cursor):
    global _status_index_ready, _publish_setting_ready
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_automation_settings (
        organization_key VARCHAR(64) PRIMARY KEY,
        rights_reply TINYINT NOT NULL DEFAULT 0, rights_cleanup TINYINT NOT NULL DEFAULT 0,
        freight TINYINT NOT NULL DEFAULT 0, publish_infringement TINYINT NOT NULL DEFAULT 0, updated_by BIGINT NOT NULL,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    if not _publish_setting_ready:
        with _schema_guard:
            if not _publish_setting_ready:
                cursor.execute("SHOW COLUMNS FROM workbench_automation_settings LIKE 'publish_infringement'")
                if not cursor.fetchone():
                    try:
                        cursor.execute("ALTER TABLE workbench_automation_settings ADD COLUMN publish_infringement TINYINT NOT NULL DEFAULT 0")
                    except Exception as exc:
                        if not exc.args or exc.args[0] != 1060:
                            raise
                _publish_setting_ready = True
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_automation_runs (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, organization_key VARCHAR(64) NOT NULL,
        rule_key VARCHAR(32) NOT NULL, job_key CHAR(64) NOT NULL, subject VARCHAR(512) NOT NULL,
        status VARCHAR(24) NOT NULL, detail_json LONGTEXT NOT NULL,
        started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX automation_scope (organization_key, id),
        INDEX automation_job (organization_key, rule_key, job_key, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_automation_run_events (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, run_id BIGINT NOT NULL,
        detail_json LONGTEXT NOT NULL,
        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        INDEX automation_run_events (run_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_automation_run_summaries (
        run_id BIGINT PRIMARY KEY,
        failure_reason VARCHAR(1200) NOT NULL DEFAULT '',
        salesperson VARCHAR(128) NOT NULL DEFAULT '',
        store_name VARCHAR(255) NOT NULL DEFAULT '',
        detail_count INT NOT NULL DEFAULT 0,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    # Existing installations predate queue/status lookups. Build this once
    # online; serialize the cross-process SHOW/ALTER pair to avoid error 1061
    # when web and worker processes start together.
    if not _status_index_ready:
        with _schema_guard:
            if not _status_index_ready:
                cursor.execute(
                    "SHOW INDEX FROM workbench_automation_runs "
                    "WHERE Key_name='automation_status'")
                if cursor.fetchone():
                    _status_index_ready = True
                else:
                    cursor.execute(
                        "SELECT GET_LOCK('workbench-automation-schema',10) AS acquired")
                    acquired = bool((cursor.fetchone() or {}).get("acquired"))
                    try:
                        if acquired:
                            cursor.execute(
                                "SHOW INDEX FROM workbench_automation_runs "
                                "WHERE Key_name='automation_status'")
                            if not cursor.fetchone():
                                cursor.execute(
                                    "ALTER TABLE workbench_automation_runs "
                                    "ADD INDEX automation_status (status,id), "
                                    "ALGORITHM=INPLACE, LOCK=NONE")
                            _status_index_ready = True
                    finally:
                        if acquired:
                            cursor.execute(
                                "SELECT RELEASE_LOCK('workbench-automation-schema')")


LOG_STATUSES = {"queued", "running", "success", "failed", "unknown", "skipped", "interrupted"}
RETRYABLE_LOG_STATUSES = {"failed", "interrupted"}
DIAGNOSTIC_LOG_STATUSES = RETRYABLE_LOG_STATUSES | {"unknown"}
MAX_MANUAL_RETRIES = 50
MAX_RETRIES_PER_CYCLE = 5
RETRY_DRAIN_BUDGET_SECONDS = 30
_RETRY_CONTEXT_KEY = "_retry_context"


def _walk_detail(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_detail(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_detail(item)


def _detail_value(detail, *keys):
    """Return the newest useful value for any key in a persisted journal."""
    found = None
    for item in _walk_detail(detail):
        for key in keys:
            if key in item and item[key] not in (None, "", [], {}):
                found = item[key]
    return found


def retry_context(rule, detail):
    """Recover replay inputs from new private snapshots or compatible old logs."""
    entries = detail if isinstance(detail, list) else [detail]
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get(_RETRY_CONTEXT_KEY), dict):
            return entry[_RETRY_CONTEXT_KEY]
    if rule == "rights_reply":
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            token_id = entry.get("token_id")
            case_id = entry.get("case_id") or entry.get("infraction_id")
            item_id = entry.get("item_id")
            if token_id and case_id and item_id:
                return {"token_id": token_id, "case_id": case_id,
                        "item_id": item_id, "due_at": entry.get("due_at")}
    elif rule == "rights_cleanup":
        for entry in entries:
            if (isinstance(entry, dict) and entry.get("token_id")
                    and isinstance(entry.get("reports"), list) and entry["reports"]):
                return {"token_id": entry["token_id"],
                        "source_ids": [row.get("source_id") for row in entry["reports"]
                                       if isinstance(row, dict) and row.get("source_id")],
                        "title": str(entry["reports"][0].get("title") or "")}
    elif rule == "freight":
        for entry in entries:
            if isinstance(entry, dict) and entry.get("token_id") and entry.get("order_number"):
                return {"token_id": entry["token_id"], "order_number": entry["order_number"]}
    return {}


def public_detail(value):
    """Remove private replay snapshots before journal data reaches the browser."""
    if isinstance(value, list):
        rows = [public_detail(item) for item in value]
        return [item for item in rows if item not in ({}, [], None)]
    if isinstance(value, dict):
        return {key: public_detail(item) for key, item in value.items()
                if key != _RETRY_CONTEXT_KEY}
    return value


def failure_reason(detail, status="failed"):
    """Extract the most useful human-readable reason from heterogeneous logs."""
    reasons = []

    def add(value):
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, (dict, list)):
                visit(item)
                continue
            text = str(item or "").strip()
            if text and text not in reasons:
                reasons.append(text)

    def visit(value):
        if isinstance(value, list):
            for item in reversed(value):
                visit(item)
            return
        if not isinstance(value, dict):
            return
        state = str(value.get("automation_status") or value.get("status") or "").strip().lower()
        failed = state in {"failed", "error", "partial", "interrupted", "unknown", "失败", "异常", "保存失败"}
        for key in ("error", "errors", "execution_error"):
            if key in value:
                add(value[key])
        if failed:
            for key in ("message", "reason"):
                if key in value:
                    add(value[key])
        for key, item in reversed(tuple(value.items())):
            if key not in {"error", "errors", "execution_error", "message", "reason", _RETRY_CONTEXT_KEY}:
                visit(item)

    visit(detail)
    if reasons:
        return "；".join(reasons[:3])[:1200]
    if status == "interrupted":
        return "上次执行异常中断，尚未完成。"
    if status == "unknown":
        return "提交结果尚未确认，请人工核对平台状态。"
    return "历史日志未提取到摘要，请展开明细查看完整失败原因。"


def retry_blocked_reason(run, settings):
    status = str(run.get("status") or "")
    rule = str(run.get("rule_key") or "")
    if not run.get("is_latest", True):
        return "已有更新的执行记录，请刷新后重试最新一次"
    if status == "unknown":
        return "执行结果待核实，为避免重复写入不能直接重试"
    if status == "running":
        return "任务仍在执行，不能重复提交"
    if status == "queued":
        return "任务已排队等待重试"
    if status not in RETRYABLE_LOG_STATUSES:
        return "只有失败或中断的任务可以重试"
    if rule not in RULES:
        return "此类日志不支持重试"
    if str(run.get("subject") or "") == "读取待执行数据失败":
        return "扫描失败会在下一轮自动恢复，无需手动重试"
    if not settings.get(rule):
        return "请先开启对应的自动规则再重试"
    return ""


def serialize_run(row, settings):
    stored_reason = str(row.get("failure_reason") or "").strip()[:1200]
    row["failure_reason"] = (stored_reason or failure_reason([], row.get("status"))
                             if row.get("status") in DIAGNOSTIC_LOG_STATUSES else "")
    row["detail_loaded"] = False
    blocked = retry_blocked_reason(row, settings)
    row["retryable"] = not blocked
    row["retry_blocked_reason"] = blocked
    row.pop("is_latest", None)
    for field in ("started_at", "updated_at"):
        row[field] = str(row[field])
    return row


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
        clauses.append(
            "(summary.salesperson LIKE %s ESCAPE '!' "
            "OR JSON_SEARCH(IF(JSON_VALID(workbench_automation_runs.detail_json),"
            "workbench_automation_runs.detail_json,'{}'),'one',%s,'!','$**.salesperson') IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM workbench_automation_run_events AS event "
            "WHERE event.run_id=workbench_automation_runs.id "
            "AND JSON_SEARCH(IF(JSON_VALID(event.detail_json),event.detail_json,'{}'),"
            "'one',%s,'!','$**.salesperson') IS NOT NULL))")
        params.extend((pattern(salesperson), pattern(salesperson), pattern(salesperson)))
    if store:
        # Historical names remain searchable after a store rename. Token IDs
        # also cover early reply logs that did not snapshot the store name.
        names = (
            "(JSON_SEARCH(IF(JSON_VALID(workbench_automation_runs.detail_json),"
            "workbench_automation_runs.detail_json,'{}'),'one',%s,'!',"
            "'$**.store_name','$**.company_store','$**.store') IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM workbench_automation_run_events AS event "
            "WHERE event.run_id=workbench_automation_runs.id "
            "AND JSON_SEARCH(IF(JSON_VALID(event.detail_json),event.detail_json,'{}'),'one',%s,'!',"
            "'$**.store_name','$**.company_store','$**.store') IS NOT NULL))")
        token_match = " OR ".join(
            "(JSON_CONTAINS(JSON_EXTRACT(IF(JSON_VALID(workbench_automation_runs.detail_json),"
            "workbench_automation_runs.detail_json,'{{}}'), '$**.{key}'), CAST(t.id AS JSON)) "
            "OR EXISTS (SELECT 1 FROM workbench_automation_run_events AS token_event "
            "WHERE token_event.run_id=workbench_automation_runs.id "
            "AND JSON_CONTAINS(JSON_EXTRACT(IF(JSON_VALID(token_event.detail_json),"
            "token_event.detail_json,'{{}}'), '$**.{key}'), CAST(t.id AS JSON))))".format(key=key)
            for key in ("token_id", "source_token_id"))
        clauses.append("(summary.store_name LIKE %s ESCAPE '!' OR " + names + " OR EXISTS (SELECT 1 FROM mercado_store_tokens t WHERE "
                       "COALESCE(NULLIF(NULLIF(t.organization_key, 'default'), ''), 'wuhan-zeshun')=%s "
                       "AND (t.display_name LIKE %s ESCAPE '!' OR CAST(t.id AS CHAR)=%s) AND (" + token_match + ")))" )
        params.extend((pattern(store), pattern(store), pattern(store), organization, pattern(store), store))
    before = max(0, int(payload.get("before") or 0))
    if before:
        clauses.append("id < %s")
        params.append(before)
    return " AND ".join(clauses), tuple(params)


def _retry_ids(data):
    if not isinstance(data, dict) or not isinstance(data.get("run_ids"), list):
        raise ValueError("请选择需要重试的执行日志")
    values = []
    for value in data["run_ids"]:
        # JSON booleans are Python ints, and int(1.5) silently truncates.  A
        # retry target must be the exact numeric primary key sent by this UI.
        if type(value) is not int:
            raise ValueError("执行日志编号无效")
        run_id = value
        if run_id <= 0:
            raise ValueError("执行日志编号无效")
        if run_id not in values:
            values.append(run_id)
    if not values:
        raise ValueError("请选择需要重试的执行日志")
    if len(values) > MAX_MANUAL_RETRIES:
        raise ValueError(f"每次最多重试 {MAX_MANUAL_RETRIES} 条日志")
    return sorted(values)


def _run_detail(cursor, organization, run_id):
    cursor.execute(
        "SELECT runs.id,runs.status,runs.detail_json,COALESCE(summary.failure_reason,'') AS failure_reason "
        "FROM workbench_automation_runs AS runs "
        "LEFT JOIN workbench_automation_run_summaries AS summary ON summary.run_id=runs.id "
        "WHERE runs.organization_key=%s AND runs.id=%s", (organization, int(run_id)))
    row = cursor.fetchone()
    if not row:
        raise ValueError("执行日志不存在或无权访问")
    try:
        base = json.loads(row.get("detail_json") or "[]")
    except (TypeError, ValueError):
        base = [{"error": "历史日志格式损坏，无法解析原始明细"}]
    entries = list(base) if isinstance(base, list) else [base]
    cursor.execute("SELECT detail_json FROM workbench_automation_run_events WHERE run_id=%s ORDER BY id", (row["id"],))
    for event in cursor.fetchall() or []:
        try:
            entries.append(json.loads(event["detail_json"]))
        except (TypeError, ValueError):
            entries.append({"error": "执行事件格式损坏，无法解析"})
    reason = str(row.get("failure_reason") or "").strip()
    if row.get("status") in DIAGNOSTIC_LOG_STATUSES and not reason:
        reason = failure_reason(entries, row.get("status"))
    if reason:
        cursor.execute(
            "INSERT INTO workbench_automation_run_summaries "
            "(run_id,failure_reason,salesperson,store_name,detail_count) VALUES (%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE "
            "failure_reason=IF(failure_reason='',VALUES(failure_reason),failure_reason),"
            "salesperson=IF(salesperson='',VALUES(salesperson),salesperson),"
            "store_name=IF(store_name='',VALUES(store_name),store_name),"
            "detail_count=GREATEST(detail_count,VALUES(detail_count))",
            (row["id"], reason, str(_detail_value(entries, "salesperson") or "")[:128],
             str(_detail_value(entries, "store_name", "company_store", "store") or "")[:255],
             len(entries)))
    return {"run_id": row["id"], "detail": public_detail(entries), "failure_reason": reason}


def _backfill_run_summaries(cursor, limit=10):
    """Incrementally index legacy failure reasons without loading them in UI lists."""
    cursor.execute(
        "SELECT runs.id,runs.status,runs.detail_json FROM workbench_automation_runs AS runs "
        "LEFT JOIN workbench_automation_run_summaries AS summary ON summary.run_id=runs.id "
        "WHERE summary.run_id IS NULL AND runs.status IN ('failed','interrupted','unknown') "
        "ORDER BY runs.id DESC LIMIT %s", (max(1, min(int(limit), 50)),))
    for row in cursor.fetchall() or []:
        try:
            detail = json.loads(row.get("detail_json") or "[]")
        except (TypeError, ValueError):
            detail = [{"error": "历史日志格式损坏，无法解析原始明细"}]
        cursor.execute(
            "INSERT IGNORE INTO workbench_automation_run_summaries "
            "(run_id,failure_reason,salesperson,store_name,detail_count) VALUES (%s,%s,%s,%s,%s)",
            (row["id"], failure_reason(detail, row.get("status")),
             str(_detail_value(detail, "salesperson") or "")[:128],
             str(_detail_value(detail, "store_name", "company_store", "store") or "")[:255],
             len(detail) if isinstance(detail, list) else 1))


def _queue_retries(cursor, organization, actor_id, settings, data):
    accepted, rejected = [], []
    for source_id in _retry_ids(data):
        cursor.execute(
            "SELECT id,rule_key,job_key FROM workbench_automation_runs "
            "WHERE organization_key=%s AND id=%s",
            (organization, source_id))
        candidate = cursor.fetchone()
        if not candidate:
            rejected.append({"source_run_id": source_id, "reason": "日志不存在或无权访问"})
            continue
        cursor.execute(
            "SELECT id,rule_key,job_key,subject,status,detail_json,TRUE AS is_latest "
            "FROM workbench_automation_runs "
            "WHERE organization_key=%s AND rule_key=%s AND job_key=%s "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE",
            (organization, candidate["rule_key"], candidate["job_key"]))
        source = cursor.fetchone() or {}
        if int(source.get("id") or 0) != source_id:
            rejected.append({"source_run_id": source_id,
                             "reason": "已有更新的执行记录，请刷新后重试最新一次"})
            continue
        blocked = retry_blocked_reason(source, settings)
        if blocked:
            rejected.append({"source_run_id": source_id, "reason": blocked})
            continue
        try:
            raw_detail = json.loads(source.get("detail_json") or "[]")
        except (TypeError, ValueError):
            raw_detail = []
        context = retry_context(source["rule_key"], raw_detail)
        initial = [{
            "time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            "stage": "手动重试已排队", "source_run_id": source_id,
            "operator_id": int(actor_id), _RETRY_CONTEXT_KEY: context,
        }]
        cursor.execute(
            "INSERT INTO workbench_automation_runs "
            "(organization_key,rule_key,job_key,subject,status,detail_json) "
            "VALUES (%s,%s,%s,%s,'queued',%s)",
            (organization, source["rule_key"], source["job_key"], source["subject"],
             json.dumps(initial, ensure_ascii=False, default=str)))
        accepted.append({"source_run_id": source_id, "run_id": cursor.lastrowid})
    return {"accepted": accepted, "rejected": rejected}


def _list_runs(cursor, organization, payload, settings):
    where, parameters = log_filter_sql(organization, payload)
    cursor.execute(
        "SELECT workbench_automation_runs.id,workbench_automation_runs.rule_key,"
        "workbench_automation_runs.subject,workbench_automation_runs.status,"
        "workbench_automation_runs.started_at,workbench_automation_runs.updated_at,"
        "COALESCE(NULLIF(summary.failure_reason,''),"
        "CASE WHEN workbench_automation_runs.status IN ('failed','interrupted','unknown') "
        "AND JSON_VALID(workbench_automation_runs.detail_json) THEN "
        "LEFT(NULLIF(JSON_UNQUOTE(COALESCE("
        "JSON_EXTRACT(workbench_automation_runs.detail_json,'$[last].error'),"
        "JSON_EXTRACT(workbench_automation_runs.detail_json,'$[last].execution_error'),"
        "JSON_EXTRACT(workbench_automation_runs.detail_json,'$[last].result.error'),"
        "JSON_EXTRACT(workbench_automation_runs.detail_json,'$[last].result.execution_error'),"
        "JSON_EXTRACT(workbench_automation_runs.detail_json,'$[last].result.message'),"
        "JSON_EXTRACT(workbench_automation_runs.detail_json,'$[last].result.errors[0]'),"
        "JSON_EXTRACT(workbench_automation_runs.detail_json,'$[last].result.links[last].errors[0]'),"
        "JSON_EXTRACT(workbench_automation_runs.detail_json,'$[last].message'))),'null'),1200) "
        "ELSE NULL END,'') AS failure_reason,"
        "COALESCE(summary.salesperson,'') AS salesperson,"
        "COALESCE(summary.store_name,'') AS store_name,"
        "COALESCE(summary.detail_count,0) AS detail_count,"
        "NOT EXISTS (SELECT 1 FROM workbench_automation_runs AS newer "
        "WHERE newer.organization_key=workbench_automation_runs.organization_key "
        "AND newer.rule_key=workbench_automation_runs.rule_key "
        "AND newer.job_key=workbench_automation_runs.job_key "
        "AND newer.id>workbench_automation_runs.id) AS is_latest "
        "FROM workbench_automation_runs "
        "LEFT JOIN workbench_automation_run_summaries AS summary "
        "ON summary.run_id=workbench_automation_runs.id WHERE " + where +
        " ORDER BY workbench_automation_runs.id DESC LIMIT 51", parameters)
    rows = list(cursor.fetchall() or [])
    more = len(rows) > 50
    rows = [serialize_run(row, settings) for row in rows[:50]]
    return rows, (rows[-1]["id"] if more else None)


def local(payload):
    from bit.knowledge_organization import normalize
    organization = normalize(payload.get("organization_key"))
    operation = payload.get("action", "list")
    retry_result = None
    connection = connect()
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            if operation == "publish_policy":
                cursor.execute("SELECT publish_infringement FROM workbench_automation_settings WHERE organization_key=%s", (organization,))
                return {"enabled": bool((cursor.fetchone() or {}).get("publish_infringement"))}
            if operation == "detail":
                result = _run_detail(cursor, organization, payload.get("run_id"))
                connection.commit()
                return result
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
            elif operation not in {"list", "retry"}:
                raise ValueError("无效自动化操作")
            cursor.execute("SELECT * FROM workbench_automation_settings WHERE organization_key=%s", (organization,))
            raw_settings = cursor.fetchone() or {}
            settings = {key: bool(raw_settings.get(key)) for key in RULES}
            if operation == "retry":
                retry_result = _queue_retries(cursor, organization, payload["actor_id"], settings, payload.get("data"))
            rows, next_before = _list_runs(cursor, organization, payload, settings)
        connection.commit()
        result = {"settings": settings, "runs": rows, "next_before": next_before,
                  "reply_text": REPLY_TEXT}
        if retry_result is not None:
            result["retry"] = retry_result
        return result
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


def begin(organization, rule, identity, subject, retry_data=None):
    """Called under the cross-process/cross-host MySQL worker lease."""
    key = hashlib.sha256(str(identity).encode()).hexdigest()
    initial = []
    if retry_data:
        initial.append({"time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                        _RETRY_CONTEXT_KEY: retry_data})
    connection = connect()
    try:
        with connection.cursor() as cursor:
            ensure_tables(cursor)
            cursor.execute("SELECT id,status, TIMESTAMPDIFF(SECOND, updated_at, CURRENT_TIMESTAMP) AS age FROM workbench_automation_runs WHERE organization_key=%s AND rule_key=%s AND job_key=%s ORDER BY id DESC LIMIT 1 FOR UPDATE", (organization, rule, key))
            previous = cursor.fetchone()
            if previous and (previous["status"] in {"success", "unknown", "queued"}
                             or int(previous["age"]) < 3600):
                return None
            if previous and previous["status"] in {"running", "queued"}:
                cursor.execute("UPDATE workbench_automation_runs SET status='interrupted' WHERE id=%s", (previous["id"],))
            cursor.execute("INSERT INTO workbench_automation_runs (organization_key,rule_key,job_key,subject,status,detail_json) VALUES (%s,%s,%s,%s,'running',%s)",
                           (organization, rule, key, str(subject)[:512],
                            json.dumps(initial, ensure_ascii=False, default=str)))
            run_id = cursor.lastrowid
        connection.commit()
        return run_id
    finally:
        connection.close()


def journal(run_id, detail, status=None):
    entry = {"time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), **detail}
    salesperson = str(_detail_value(entry, "salesperson") or "")[:128]
    store_name = str(_detail_value(entry, "store_name", "company_store", "store") or "")[:255]
    update_reason = status is not None
    reason = failure_reason(entry, status) if status in DIAGNOSTIC_LOG_STATUSES else ""
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO workbench_automation_run_events (run_id,detail_json) VALUES (%s,%s)",
                           (run_id, json.dumps(entry, ensure_ascii=False, default=str)))
            cursor.execute(
                "INSERT INTO workbench_automation_run_summaries "
                "(run_id,failure_reason,salesperson,store_name,detail_count) VALUES (%s,%s,%s,%s,1) "
                "ON DUPLICATE KEY UPDATE detail_count=detail_count+1,"
                "salesperson=IF(VALUES(salesperson)<>'',VALUES(salesperson),salesperson),"
                "store_name=IF(VALUES(store_name)<>'',VALUES(store_name),store_name),"
                "failure_reason=IF(%s,VALUES(failure_reason),failure_reason)",
                (run_id, reason, salesperson, store_name, bool(update_reason)))
            cursor.execute("UPDATE workbench_automation_runs SET status=COALESCE(%s,status) WHERE id=%s",
                           (status, run_id))
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


def execute_job(organization, rule, identity, subject, operation, retry_data=None):
    if not enabled(organization, rule):
        return
    run_id = begin(organization, rule, identity, subject, retry_data)
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


def _scoped_tokens(organization):
    from bit import bit_db_api
    from bit.knowledge_organization import normalize
    rows = bit_db_api.list_mercado_store_tokens().get("rows", [])
    return [row for row in rows if bool(row.get("enabled", 1))
            and normalize(row.get("organization_key") or "wuhan-zeshun") == organization]


def _retry_reply(run, context):
    from bit import bit_db_api
    from bit.mercado_action_center import _rights_holder_replies
    tokens = _scoped_tokens(run["organization_key"])
    token_map = {int(row["id"]): row for row in tokens}
    candidates = _rights_holder_replies(bit_db_api, sorted(token_map), include_replied=True)
    wanted_token = int(context.get("token_id") or 0)
    wanted_case = str(context.get("case_id") or "")
    wanted_item = str(context.get("item_id") or "")
    selected = None
    for row in candidates:
        identity = f"{row['token_id']}:{row['infraction_id']}"
        direct = (wanted_token and int(row.get("token_id") or 0) == wanted_token
                  and str(row.get("infraction_id") or "") == wanted_case
                  and str(row.get("item_id") or "") == wanted_item)
        if direct or hashlib.sha256(identity.encode()).hexdigest() == run["job_key"]:
            selected = row
            break
    if not selected or int(selected["token_id"]) not in token_map:
        raise ValueError("原权利人回复事项已不存在或不再属于当前企业")
    return reply(token_map[int(selected["token_id"])], selected, run["id"])


def _current_cleanup_reports(token_id):
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT source_id,item_id,title,rights_holder,reason,salesperson,site_id "
                "FROM erp_mercadolibre_infractions WHERE token_id=%s "
                "AND source_type='rights_holder' "
                "AND (reason_code IS NULL OR reason_code <> 'LEGACY_PAGE') "
                "AND resolution_status <> 'appeal_success' "
                "AND (is_current=1 OR resolution_status='appeal_failed')", (token_id,))
            return list(cursor.fetchall() or [])
    finally:
        connection.close()


def _cleanup_identity(token_id, title, rows, targets):
    """Fingerprint every fact that changes whether a same-title link is safe."""
    from bit.bit_store_link_remote_update import _is_site_listing_id
    target_key = []
    for link in targets:
        if not link.get("is_current", 1) or link.get("status") == "deleted":
            continue
        base = (int(link["token_id"]), str(link["item_id"]))
        enabled = bool(link.get("store_enabled", 1))
        valid = _is_site_listing_id(link.get("item_id"), link.get("site_id"))
        # Preserve the legacy fingerprint for ordinary targets so deploying
        # this fix cannot replay every historical success. Only a blocked
        # target carries state; correcting that state produces a new job.
        target_key.append(base if enabled and valid else
                          (*base, "blocked", str(link.get("site_id") or ""), enabled, valid))
    target_key.sort(key=lambda value: tuple(str(item) for item in value))
    return json.dumps([int(token_id), title,
                       sorted(str(row["source_id"]) for row in rows), target_key])


def _retry_cleanup(run, context):
    from erp.rights_holder_policy import same_title_links
    tokens = _scoped_tokens(run["organization_key"])
    token_map = {int(row["id"]): row for row in tokens}
    wanted_token = int(context.get("token_id") or 0)
    source_ids = {str(value) for value in context.get("source_ids") or [] if value not in (None, "")}
    # An explicitly empty historical title means the original cleanup had to
    # fetch the authoritative title from Mercado. Do not turn the display
    # subject (the item ID fallback) into a false title constraint on retry.
    wanted_title = str(
        context.get("title") if "title" in context else run.get("subject") or ""
    ).strip()

    if wanted_token:
        if wanted_token not in token_map:
            raise ValueError("原举报店铺已不属于当前企业")
        rows = _current_cleanup_reports(wanted_token)
        if source_ids:
            rows = [row for row in rows if str(row.get("source_id") or "") in source_ids]
        if wanted_title:
            rows = [row for row in rows if str(row.get("title") or "").strip() == wanted_title]
        if not rows:
            raise ValueError("原举报事项已处理、已失效或标题已变化")
        return cleanup(token_map[wanted_token], rows, run["id"])

    # Very old logs may lack a compact context. Rebuild each current identity
    # and only accept an exact job fingerprint match.
    for token_id, token in token_map.items():
        groups = {}
        for row in _current_cleanup_reports(token_id):
            groups.setdefault(str(row.get("title") or "").strip() or row["item_id"], []).append(row)
        for title, rows in groups.items():
            targets = same_title_links(title, run["organization_key"])
            identity = _cleanup_identity(token_id, title, rows, targets)
            if hashlib.sha256(identity.encode()).hexdigest() == run["job_key"]:
                return cleanup(token, rows, run["id"])
    raise ValueError("旧日志缺少可核验的举报重试参数")


def _retry_freight(run, context):
    from bit import bit_db_api
    tokens = _scoped_tokens(run["organization_key"])
    token_ids = sorted(int(row["id"]) for row in tokens)
    wanted_token = int(context.get("token_id") or 0)
    wanted_order = str(context.get("order_number") or "")
    if wanted_token and wanted_token not in token_ids:
        raise ValueError("原运费记录店铺已不属于当前企业")
    page, records = 1, []
    while True:
        data = bit_db_api.list_weight_dimensions_records(
            filters={"store_ids": token_ids}, page=page, page_size=100)
        batch = data.get("records", [])
        records.extend(batch)
        if not batch or page * 100 >= int(data.get("record_total") or 0):
            break
        page += 1
    changed = False
    for row in latest_freight_records(records, token_ids):
        if wanted_token and (int(row.get("_token_id") or 0) != wanted_token
                             or str(row.get("order_number") or "") != wanted_order):
            continue
        identity = json.dumps([row.get(key) for key in (
            "order_number", "actual_weight_g", "actual_dimensions_cm", "marketplace_item_id")], default=str)
        if hashlib.sha256(identity.encode()).hexdigest() == run["job_key"]:
            return freight(run["organization_key"], row, token_ids, run["id"])
        changed = True
    if changed:
        raise ValueError("运费记录的重量、尺寸或关联链接已变化，请等待新记录自动执行")
    raise ValueError("原运费变动记录已不存在或不再是同商品的最新记录")


def execute_queued_retry(run):
    try:
        require_enabled(run["organization_key"], run["rule_key"])
        try:
            raw_detail = json.loads(run.get("detail_json") or "[]")
        except (TypeError, ValueError):
            raw_detail = []
        context = retry_context(run["rule_key"], raw_detail)
        if run["rule_key"] == "rights_reply":
            result = _retry_reply(run, context)
        elif run["rule_key"] == "rights_cleanup":
            result = _retry_cleanup(run, context)
        elif run["rule_key"] == "freight":
            result = _retry_freight(run, context)
        else:
            raise ValueError("此类日志不支持重试")
        if not isinstance(result, dict):
            raise ValueError("重试未返回执行结果")
        status = str(result.get("automation_status") or "success")
        if status not in LOG_STATUSES - {"queued", "running"}:
            status = "failed"
        journal(run["id"], {"result": result}, status)
    except Exception as exc:
        logging.exception("自动化手动重试失败：%s", run.get("subject"))
        journal(run["id"], {"error": str(exc)}, "failed")


def _recover_interrupted_runs(cursor):
    """Reconcile executions orphaned by a worker exit while holding the lease.

    The advisory lock guarantees that no previous automation cycle is still
    executing when this runs. Reply submission is not safely repeatable after
    a crash, so it becomes ``unknown``; the idempotent cleanup/freight jobs can
    be exposed as interrupted and explicitly retried against current data.
    """
    cursor.execute(
        "UPDATE workbench_automation_runs SET status=CASE "
        "WHEN rule_key='rights_reply' THEN 'unknown' ELSE 'interrupted' END "
        "WHERE status='running' AND rule_key IN ('rights_reply','rights_cleanup','freight')")


def _claim_queued_retry(cursor):
    cursor.execute(
        "SELECT id FROM workbench_automation_runs "
        "WHERE status='queued' ORDER BY id LIMIT 1")
    candidate = cursor.fetchone() or {}
    if not candidate:
        return None
    cursor.execute(
        "SELECT id,organization_key,rule_key,job_key,subject,detail_json "
        "FROM workbench_automation_runs WHERE id=%s AND status='queued' FOR UPDATE",
        (candidate["id"],))
    row = cursor.fetchone()
    if row:
        cursor.execute(
            "UPDATE workbench_automation_runs SET status='running' "
            "WHERE status='queued' AND id=%s", (row["id"],))
    return row


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
            _recover_interrupted_runs(cursor)
            _backfill_run_summaries(cursor)
            cursor.execute("SELECT * FROM workbench_automation_settings WHERE rights_reply=1 OR rights_cleanup=1 OR freight=1")
            settings = list(cursor.fetchall() or [])
        # Keep the advisory worker lease, but release the read transaction before
        # invoking jobs that open their own connections and write these tables.
        connection.commit()
        # Scan scheduled work first: one manual freight retry can legitimately
        # spend minutes waiting for remote write verification and must not delay
        # time-sensitive rules such as a rights-holder reply near its deadline.
        if settings:
            tokens = bit_db_api.list_mercado_store_tokens().get("rows", [])
            for setting in settings:
                organization = setting["organization_key"]
                scoped = [t for t in tokens if bool(t.get("enabled", 1))
                          and normalize(t.get("organization_key") or "wuhan-zeshun") == organization]
                ids = [int(t["id"]) for t in scoped]
                if not ids:
                    continue
                for rule in SCHEDULED_RULES:
                    if not setting[rule]:
                        continue
                    try:
                        scan_rule(rule, connection, organization, scoped, ids)
                    except Exception as exc:
                        logging.exception("自动规则扫描失败：%s / %s", organization, rule)
                        run_id = begin(organization, rule, "scan:" + datetime.now(timezone.utc).strftime("%Y-%m-%d-%H"), "读取待执行数据失败")
                        if run_id:
                            journal(run_id, {"error": str(exc)}, "failed")

        # Claim only the item that is about to execute. The budget is checked
        # between durable jobs because interrupting an in-flight remote write is
        # unsafe; the remainder stays queued for the next scheduler cycle.
        retry_started_at = time.monotonic()
        for retry_index in range(MAX_RETRIES_PER_CYCLE):
            if retry_index and time.monotonic() - retry_started_at >= RETRY_DRAIN_BUDGET_SECONDS:
                break
            with connection.cursor() as cursor:
                retry_run = _claim_queued_retry(cursor)
            connection.commit()
            if not retry_run:
                break
            execute_queued_retry(retry_run)

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

    @blueprint.route("/api/automation-center", methods=["GET", "PATCH", "POST"])
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
            action = ("save" if request.method == "PATCH" else
                      "retry" if request.method == "POST" else
                      "detail" if request.args.get("run_id") else "list")
            result = bit_db_api._mercado_action_center_call("automation_center", {
                "organization_key": organization, "actor_id": user["id"],
                "action": action, "run_id": request.args.get("run_id"),
                "data": request.get_json(silent=True), "before": request.args.get("before", 0),
                "filters": {key: request.args.get(key, "") for key in ("salesperson", "store", "type", "date_from", "date_to", "status")},
            })
            if request.method == "POST":
                retry = result.get("retry") or {}
                accepted = list(retry.get("accepted") or [])
                rejected = list(retry.get("rejected") or [])
                result = {
                    "requested_count": len(accepted) + len(rejected),
                    "queued_count": len(accepted),
                    "skipped_count": len(rejected),
                    "items": [
                        {"run_id": row["source_run_id"], "status": "queued",
                         "retry_run_id": row["run_id"]} for row in accepted
                    ] + [
                        {"run_id": row["source_run_id"], "status": "skipped",
                         "reason": row["reason"]} for row in rejected
                    ],
                }
            response = jsonify(status="success", data=result)
            response.headers["Cache-Control"] = "no-store"
            return response, (202 if request.method == "POST" else 200)
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
                "id,title,weight_g,package_length_cm,package_width_cm,package_height_cm,"
                "source_type,source_snapshot_json,review_status")
            assignment = ",".join(f"`{field}`=%s" for field in fields)
            if before:
                cursor.execute(f"UPDATE `{products.PRODUCT_TABLE}` SET {assignment}, weight_basis='mercado_remote_update', profitability_updated_at=NULL WHERE organization_key=%s AND id IN ({','.join(['%s']*len(before))})", tuple(fields.values()) + (organization,) + tuple(r["id"] for r in before))
                for product in before:
                    changed = any(
                        products._decimal_from_text(product.get(field))
                        != products._decimal_from_text(value)
                        for field, value in fields.items()
                    )
                    if not changed or str(product.get("source_type") or "").lower() != "ai_original":
                        continue
                    snapshot = products._loads(product.get("source_snapshot_json"), {})
                    snapshot = snapshot if isinstance(snapshot, dict) else {}
                    products._mark_ai_marketplace_drafts_unreviewed(snapshot)
                    cursor.execute(
                        f"UPDATE `{products.PRODUCT_TABLE}` SET `source_snapshot_json`=%s, "
                        "`review_status`='unreviewed' WHERE `id`=%s",
                        (products._dumps(snapshot), product["id"]),
                    )
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
                            lambda run, r=row: reply(token_map[int(r["token_id"])], r, run),
                            retry_data={"token_id": row["token_id"], "case_id": row["infraction_id"],
                                        "item_id": row["item_id"], "due_at": row.get("due_at")})
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
                identity = _cleanup_identity(token["id"], title, rows, targets)
                execute_job(organization, "rights_cleanup", identity, title,
                            lambda run, t=token, r=rows: cleanup(t, r, run),
                            retry_data={"token_id": token["id"],
                                        "title": str(rows[0].get("title") or "").strip(),
                                        "source_ids": [row["source_id"] for row in rows]})
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
                        lambda run, r=row: freight(organization, r, ids, run),
                        retry_data={"token_id": row.get("_token_id"),
                                    "order_number": row.get("order_number")})
