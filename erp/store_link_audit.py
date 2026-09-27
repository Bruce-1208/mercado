"""Durable append-only store-link operation journal (one database per executor)."""
from __future__ import annotations

import base64
import hmac
import contextvars
import functools
import inspect
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from datetime import datetime, timezone

_context = contextvars.ContextVar("store_link_audit", default=None)
_SECRET = {"authorization", "cookie", "password", "secret", "access_token", "refresh_token", "api_key", "client_secret"}


def safe(value):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if any(s in str(k).lower() for s in _SECRET) else safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe(v) for v in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return safe(json.loads(value))
        except (ValueError, TypeError):
            pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "filename"):
        return {"filename": value.filename, "content_type": value.content_type}
    if callable(value):
        return "[callable]"
    return str(value) if type(value).__module__ in {"decimal", "datetime"} else f"[{type(value).__name__}]"


def connect():
    path = Path(os.environ.get("MERCADO_STORE_LINK_AUDIT_PATH") or Path(__file__).resolve().parents[1] / "runtime_logs" / "store_link_operations.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS operations (id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, trace_id TEXT NOT NULL, created_at TEXT NOT NULL, action TEXT NOT NULL, phase TEXT NOT NULL, details TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS operation_trace ON operations(trace_id, id)")
    return db


def record(action, phase, details, operation_id=None):
    context = _context.get() or {"trace_id": uuid.uuid4().hex, "actor": {"type": "system"}}
    db = connect()
    try:
        with db:
            db.execute("INSERT INTO operations(operation_id,trace_id,created_at,action,phase,details) VALUES(?,?,?,?,?,?)", (operation_id or uuid.uuid4().hex, context["trace_id"], datetime.now(timezone.utc).isoformat(), action, phase, json.dumps(safe({"context": context, **details}), ensure_ascii=False)))
    finally:
        db.close()


# Classify at query time so records written before the module existed remain searchable.
_OPERATION_ACTIONS = {
    "sync": ("replace_store_snapshot", "finalize_store_snapshot", "request_store_link_sync", "mark_store_link_sync_started", "mark_store_link_sync_finished", "_sync_store", "run_store_link_sync", "start_store_link_sync"),
    "update": ("bulk_update_store_links", "_update_one_link", "remote_update_item", "run_store_link_remote_update", "start_store_link_remote_update"),
    "delete": ("delete_store_links", "mark_store_links_deleted", "_delete_one_link"),
    "advertising": ("advertise_store_link", "advertise_store_links", "mark_advertising_enabled", "mark_advertising_status"),
    "video": ("upload_store_link_video", "mark_video_uploaded"),
}
OPERATION_TYPES = {
    "sync": "店铺同步", "update": "链接修改", "delete": "链接删除",
    "advertising": "广告操作", "video": "视频上传", "query": "查询与状态读取", "other": "其他操作",
}


def _operation_type_sql():
    cases = ["CASE"]
    for category, actions in _OPERATION_ACTIONS.items():
        names = ", ".join("'" + action + "'" for action in actions)
        cases.append(f"WHEN action IN ({names}) THEN '{category}'")
    path = "json_extract(details, '$.context.path')"
    cases.append("WHEN action = 'http_request' THEN CASE")
    cases.append(f"WHEN json_extract(details, '$.context.method') IN ('GET', 'HEAD', 'OPTIONS') OR {path} LIKE '%/category-paths' THEN 'query'")
    for suffix, category in (("/sync/start", "sync"), ("/bulk-update", "update"), ("/delete", "delete"), ("/advertise", "advertising"), ("/bulk-advertise", "advertising"), ("/video", "video")):
        cases.append(f"WHEN {path} LIKE '%{suffix}' THEN '{category}'")
    cases.append("ELSE 'other' END ELSE 'other' END")
    return " ".join(cases)


def _utc_history_bound(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("无效的操作时间范围") from exc
    if parsed.tzinfo is None:
        raise ValueError("操作时间范围必须包含时区")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def history(before_id=None, limit=100, trace_id=None, operation_type=None,
            salesperson=None, created_after=None, created_before=None):
    limit = max(1, min(int(limit), 500))
    if operation_type and operation_type not in OPERATION_TYPES:
        raise ValueError("无效的操作类型")
    created_after = _utc_history_bound(created_after)
    created_before = _utc_history_bound(created_before)
    if created_after and created_before and created_after >= created_before:
        raise ValueError("操作开始时间必须早于结束时间")
    category_sql = _operation_type_sql()
    clauses, params = [], []
    if operation_type:
        clauses.append(f"({category_sql}) = ?")
        params.append(operation_type)
    salesperson = str(salesperson or "").strip()
    if salesperson:
        clauses.append(
            "(instr(lower(COALESCE(json_extract(details, '$.context.actor.display_name'), '')), lower(?)) > 0 "
            "OR instr(lower(COALESCE(json_extract(details, '$.context.actor.username'), '')), lower(?)) > 0)"
        )
        params.extend((salesperson, salesperson))
    if created_after:
        clauses.append("created_at >= ?")
        params.append(created_after)
    if created_before:
        clauses.append("created_at < ?")
        params.append(created_before)
    if before_id:
        clauses.append("id < ?")
        params.append(int(before_id))
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(trace_id)
    db = connect()
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(f"SELECT *, ({category_sql}) AS operation_type FROM operations" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]
    finally:
        db.close()


def audited(fn):
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        token = None
        if _context.get() is None:
            token = _context.set({"trace_id": uuid.uuid4().hex, "actor": {"type": "system"}})
        operation_id, started = uuid.uuid4().hex, time.monotonic()
        try:
            arguments = signature.bind(*args, **kwargs).arguments
            record(fn.__name__, "started", {"arguments": arguments}, operation_id)
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                record(fn.__name__, "failed", {"error_type": type(exc).__name__, "error": str(exc), "duration_ms": round((time.monotonic() - started) * 1000)}, operation_id)
                raise
            record(fn.__name__, ("failed" if result.get("status") == "error" else "partial" if result.get("status") == "partial" else "completed") if isinstance(result, dict) else "completed", {"result": result, "duration_ms": round((time.monotonic() - started) * 1000)}, operation_id)
            return result
        finally:
            if token is not None:
                _context.reset(token)
    return wrapped


def contextual_target(fn):
    """Capture initiating identity before dispatching to a worker thread."""
    context = contextvars.copy_context()
    return lambda *args, **kwargs: context.run(fn, *args, **kwargs)


def forwarding_headers():
    context = _context.get()
    if not context:
        return {}
    payload = {"trace_id": context["trace_id"], "actor": context["actor"]}
    return {"X-Store-Link-Audit": base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()}


def trusted_forwarded_context(request):
    secret = os.environ.get("BIT_DB_API_TOKEN", "").strip()
    supplied = request.headers.get("X-Internal-Token", "")
    if not request.path.startswith("/api/db/store-links") or not secret or not hmac.compare_digest(secret, supplied):
        return None
    encoded = request.headers.get("X-Store-Link-Audit", "")
    if not encoded or len(encoded) > 8192:
        return None
    try:
        context = json.loads(base64.urlsafe_b64decode(encoded))
        trace_id = str(uuid.UUID(hex=context["trace_id"])).replace("-", "")
        actor = {k: v for k, v in context["actor"].items() if k in {"id", "username", "display_name", "type"}}
        return {"trace_id": trace_id, "actor": actor}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def install_request_audit(app):
    from flask import g, request, session

    @app.before_request
    def begin_store_link_request():
        if not (request.path.startswith("/api/store-links") or request.path.startswith("/api/db/store-links")) or request.path.endswith("/operation-logs"):
            return
        g.store_link_audit_token = _context.set({"trace_id": uuid.uuid4().hex, "actor": {k: v for k, v in (session.get("workbench_user") or {"type": "service_or_anonymous"}).items() if k in {"id", "username", "display_name", "type"}}, "method": request.method, "path": request.path, "remote_addr": request.remote_addr})
        forwarded = trusted_forwarded_context(request)
        if forwarded:
            _context.get().update(forwarded)
        g.store_link_operation_id = uuid.uuid4().hex
        g.store_link_started = time.monotonic()
        record("http_request", "started", {"query": request.args.to_dict(flat=False), "body": request.get_json(silent=True), "form": request.form.to_dict(flat=False), "files": {k: [safe(v) for v in request.files.getlist(k)] for k in request.files}}, g.store_link_operation_id)

    @app.after_request
    def finish_store_link_request(response):
        if hasattr(g, "store_link_operation_id"):
            record("http_request", "completed" if response.status_code < 400 else "failed", {"http_status": response.status_code, "response": response.get_json(silent=True), "duration_ms": round((time.monotonic() - g.store_link_started) * 1000)}, g.store_link_operation_id)
            response.headers["X-Store-Link-Trace-Id"] = _context.get()["trace_id"]
        return response

    @app.teardown_request
    def cleanup_store_link_request(error):
        token = g.pop("store_link_audit_token", None)
        if token is not None:
            try:
                if error:
                    record("http_request", "failed", {"error_type": type(error).__name__, "error": str(error)}, g.store_link_operation_id)
            finally:
                _context.reset(token)
