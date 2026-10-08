"""Append-only AI usage ledger, provider-price snapshots and durable client outbox.

Only provider-reported usage is counted. Prompts, API keys and response content
never enter this journal. HTTP routes must supply their authenticated enterprise
and visible user IDs; the local dispatcher does not establish authorization.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid


_context = ContextVar("ai_usage_billing", default=None)
_context_resolver = None
_outbox_lock = threading.Lock()
_worker_start_lock = threading.Lock()
_schema_lock = threading.Lock()
_initialized_paths = set()
_outbox_worker = None
_outbox_wake = threading.Event()
CN_TIMEZONE = timezone(timedelta(hours=8))
DEFAULT_ORGANIZATION = "wuhan-zeshun"
RATE_VERSION = "aliyun-beijing-2026-10-07"
TASK_TYPES = {
    "ai_original": "AI 原创商品", "risk_check": "侵权风险检查",
    "infringement_knowledge": "侵权知识分析", "ai_appeal": "AI 申诉",
    "ai_weight_price": "AI 核价核重", "product_copy": "商品文案",
    "translation": "AI 翻译", "ai_video": "AI 视频", "other": "其他 AI 任务",
    "product_review": "商品审核",
}


def set_usage_context_resolver(callback):
    """Register a cheap request-identity resolver without importing the Flask app."""
    global _context_resolver
    _context_resolver = callback


def current_usage_context():
    fallback = {"actor_id": os.environ.get("AI_BILLING_ACTOR_ID") or 0,
                "actor_name": os.environ.get("AI_BILLING_ACTOR_NAME") or "系统任务",
                "organization_key": os.environ.get("AI_BILLING_ORGANIZATION_KEY") or
                    os.environ.get("WORKBENCH_DEFAULT_ORGANIZATION_KEY") or DEFAULT_ORGANIZATION,
                "task_id": os.environ.get("BIT_EXECUTION_JOB_ID") or "",
                "agent_job_id": os.environ.get("BIT_EXECUTION_JOB_ID") or ""}
    resolved = {}
    if _context_resolver is not None:
        try:
            resolved = dict(_context_resolver() or {})
        except Exception:
            logging.getLogger(__name__).debug("AI billing identity resolver unavailable", exc_info=True)
    return {**fallback, **resolved, **dict(_context.get() or {})}


@contextmanager
def usage_context(**fields):
    token = _context.set({**current_usage_context(), **fields})
    try:
        yield current_usage_context()
    finally:
        _context.reset(token)


def bind_usage(function, **fields):
    """Capture the initiating identity before crossing a thread boundary."""
    captured = {**current_usage_context(), **fields}
    if inspect.isgeneratorfunction(function):
        @wraps(function)
        def generate(*args, **kwargs):
            with usage_context(**captured):
                yield from function(*args, **kwargs)
        return generate
    @wraps(function)
    def run(*args, **kwargs):
        with usage_context(**captured):
            return function(*args, **kwargs)
    return run


def _path():
    return Path(os.environ.get("AI_USAGE_BILLING_PATH") or
                Path(__file__).resolve().parents[1] / "runtime_logs" / "ai_usage_billing.sqlite3")


def _connect():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _schema_lock:
        existed = path.exists()
        db = sqlite3.connect(str(path), timeout=30)
        db.row_factory = sqlite3.Row
        path_key = str(path.resolve())
        if existed and path_key in _initialized_paths:
            return db
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
        CREATE TABLE IF NOT EXISTS ai_usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE, organization_key TEXT NOT NULL,
            created_at TEXT NOT NULL, actor_id INTEGER NOT NULL,
            actor_name TEXT NOT NULL, salesperson TEXT NOT NULL,
            task_type TEXT NOT NULL, task_name TEXT NOT NULL, task_id TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, request_id TEXT NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
            total_tokens INTEGER, usage_known INTEGER NOT NULL,
            estimated_cost_cny REAL, payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ai_usage_scope_date
            ON ai_usage_records(organization_key, actor_id, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ai_usage_scope_task
            ON ai_usage_records(organization_key, task_type, model);
        CREATE UNIQUE INDEX IF NOT EXISTS ai_usage_provider_request
            ON ai_usage_records(organization_key, provider, request_id)
            WHERE request_id != '';
        CREATE TABLE IF NOT EXISTS ai_usage_outbox (
            record_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT, last_error TEXT NOT NULL DEFAULT ''
        );
            """)
        except Exception:
            db.close()
            raise
        _initialized_paths.add(path_key)
    return db


def _mapping(value):
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return dict(value.model_dump() or {})
    if hasattr(value, "to_dict"):
        return dict(value.to_dict() or {})
    if hasattr(value, "json"):
        try:
            decoded = value.json()
            return decoded if isinstance(decoded, dict) else json.loads(decoded)
        except (ValueError, TypeError):
            pass
    try:
        return vars(value)
    except TypeError:
        return {}


def _tokens(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        count = Decimal(str(value))
        return int(count) if count.is_finite() and count >= 0 and count == int(count) else None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def _first_token(*values):
    for value in values:
        parsed = _tokens(value)
        if parsed is not None:
            return parsed
    return None


def parse_usage(response=None, *, usage=None):
    """Understand OpenAI/DashScope usage objects without guessing from text size."""
    raw = _mapping(response)
    if usage is None:
        usage = raw.get("usage", getattr(response, "usage", None))
        if usage is None and any(key in raw for key in (
                "input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "total_tokens",
                "prompt_eval_count", "eval_count")):
            usage = raw
    data = _mapping(usage)
    prompt_details = _mapping(data.get("prompt_tokens_details"))
    input_details = _mapping(data.get("input_tokens_details"))
    input_tokens = _first_token(data.get("input_tokens"), data.get("prompt_tokens"), data.get("prompt_eval_count"))
    output_tokens = _first_token(data.get("output_tokens"), data.get("completion_tokens"), data.get("eval_count"))
    cached = _first_token(data.get("cached_tokens"), prompt_details.get("cached_tokens"),
                          input_details.get("cached_tokens"), data.get("prompt_cache_hit_tokens"))
    if input_tokens is None and data.get("prompt_cache_hit_tokens") is not None and data.get("prompt_cache_miss_tokens") is not None:
        hit, miss = _tokens(data["prompt_cache_hit_tokens"]), _tokens(data["prompt_cache_miss_tokens"])
        if hit is not None and miss is not None:
            input_tokens = hit + miss
    total = _tokens(data.get("total_tokens"))
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    known = total is not None or input_tokens is not None or output_tokens is not None
    consistent = not (cached is not None and input_tokens is not None and cached > input_tokens)
    if total is not None:
        consistent = consistent and not (input_tokens is not None and input_tokens > total)
        consistent = consistent and not (output_tokens is not None and output_tokens > total)
        if input_tokens is not None and output_tokens is not None:
            consistent = consistent and total == input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cached_tokens": cached if cached is not None else (0 if input_tokens is not None else None),
            "total_tokens": total, "usage_known": bool(known and consistent), "usage_consistent": consistent}


def _organization(value):
    key = str(value or DEFAULT_ORGANIZATION).strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{2,64}", key):
        raise ValueError("AI 账单缺少有效企业标识")
    return DEFAULT_ORGANIZATION if key == "default" else key


def _host(base_url):
    try:
        return (urlsplit(str(base_url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _rate_catalog():
    catalog = {
        "qwen3.7-flash": {"tiers": [(32000, .2, .04, .8), (256000, .6, .12, 2.4), (1000000, 1.2, .24, 4.8)],
                            "source": "https://help.aliyun.com/zh/model-studio/qwen3-7-flash"},
        "qwen3-vl-flash": {"tiers": [(32000, .15, .03, 1.5), (128000, .3, .06, 3), (256000, .6, .12, 6)],
                            "source": "https://help.aliyun.com/zh/model-studio/qwen3-vl-flash"},
        "qwen3-vl-plus": {"tiers": [(32000, 1, .2, 10), (128000, 1.5, .3, 15), (256000, 3, .6, 30)],
                           "source": "https://help.aliyun.com/zh/model-studio/qwen3-vl-plus"},
    }
    for rate in catalog.values():
        rate.update(host="dashscope.aliyuncs.com", version=RATE_VERSION, currency="CNY")
    # Overrides require an explicit endpoint host so a proxy cannot inherit a
    # provider's public price accidentally. Prices are CNY per million tokens.
    configured = os.environ.get("AI_USAGE_BILLING_RATES_JSON", "")
    config_path = os.environ.get("AI_USAGE_BILLING_RATES_PATH", "")
    if config_path:
        try:
            configured = Path(config_path).read_text(encoding="utf-8")
        except OSError:
            logging.getLogger(__name__).warning("AI billing rate configuration unavailable")
    if configured:
        try:
            updates = json.loads(configured)
            if not isinstance(updates, dict):
                raise ValueError("rates must be an object")
            catalog.update(updates)
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning("AI billing rate configuration invalid")
    return catalog


def estimate_cost(usage, *, model="", provider="", base_url=""):
    """Return a price and immutable rate snapshot, or an explicit unknown price."""
    unknown = {"estimated_cost_cny": None, "cost_known": False, "rate_metadata": {}, "cost_status": "unpriced"}
    if usage.get("usage_consistent") is False:
        return {**unknown, "cost_status": "inconsistent_usage"}
    if not usage.get("usage_known"):
        return {**unknown, "cost_status": "unknown_usage"}
    input_count, output_count = usage.get("input_tokens"), usage.get("output_tokens")
    if input_count is None or output_count is None:
        return {**unknown, "cost_status": "incomplete_usage"}
    model_key = str(model or "").strip()
    snapshot_alias = re.fullmatch(r"(qwen3\.7-flash|qwen3-vl-flash|qwen3-vl-plus)-\d{4}-\d{2}-\d{2}", model_key)
    rate = _rate_catalog().get(model_key) or (_rate_catalog().get(snapshot_alias.group(1)) if snapshot_alias else None)
    if not isinstance(rate, dict) or not rate.get("host") or _host(base_url) != str(rate["host"]).lower():
        return unknown
    try:
        for ceiling, input_price, cached_price, output_price in rate.get("tiers", []):
            if input_count <= int(ceiling):
                prices = [Decimal(str(value)) for value in (input_price, cached_price, output_price)]
                if not all(value.is_finite() and value >= 0 for value in prices):
                    return unknown
                cached_count = usage.get("cached_tokens") or 0
                if cached_count > input_count:
                    return {**unknown, "cost_status": "inconsistent_usage"}
                amount = (Decimal(input_count - cached_count) * prices[0] + Decimal(cached_count) * prices[1]
                          + Decimal(output_count) * prices[2]) / Decimal(1000000)
                snapshot = {"version": str(rate.get("version") or "configured"), "currency": "CNY",
                            "unit": "per_million_tokens", "endpoint_host": _host(base_url),
                            "source": str(rate.get("source") or "configured"), "input_limit": int(ceiling),
                            "input_rate": float(prices[0]), "cached_input_rate": float(prices[1]),
                            "output_rate": float(prices[2])}
                return {"estimated_cost_cny": float(amount.quantize(Decimal("0.000000000001"))),
                        "cost_known": True, "rate_metadata": snapshot, "cost_status": "estimated"}
    except (ValueError, TypeError, InvalidOperation):
        return unknown
    return unknown


def _infer_task():
    matches = (("ai_original", ("ai_original", "ai_generation_budget")),
               ("infringement_knowledge", ("infringement_knowledge_analysis",)),
               ("ai_appeal", ("ai_appeal", "appeal_ai")),
               ("ai_weight_price", ("ai_weight_price", "bit_zying_check_price")),
               ("risk_check", ("check_risk", "bit_risk", "zying_infringement", "collection_ai_check", "bit_zying_check", "bit_yuanyou")),
               ("translation", ("translation", "DeepSeekApi")), ("product_copy", ("collection_copy",)),
               ("product_review", ("listing_review",)),
               ("ai_video", ("ai_video",)))
    frames = inspect.stack(context=0)
    try:
        for frame in frames[2:]:
            filename = frame.filename.replace("\\", "/")
            for task_type, parts in matches:
                if any(part in filename for part in parts):
                    return task_type, TASK_TYPES[task_type]
        return "other", TASK_TYPES["other"]
    finally:
        del frames


def _created_at(value=None):
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=CN_TIMEZONE)
            return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        except ValueError as exc:
            raise ValueError("AI 账单记录时间无效") from exc
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_record(payload):
    record = dict(payload or {})
    parsed = parse_usage(record.get("usage") or record)
    if "usage_known" in record:
        parsed["usage_known"] = bool(record["usage_known"] and parsed["usage_consistent"])
    # Never retain arbitrary SDK responses/prompts or caller secrets.
    allowed = ("record_id", "organization_key", "created_at", "actor_id", "actor_name", "salesperson",
               "account_scope", "task_type", "task_name", "task_id", "provider", "model", "request_id",
               "agent_job_id", "product_id", "status", "endpoint_host", "base_url", "estimated_cost_cny", "cost_known",
               "rate_metadata", "cost_status")
    clean = {key: record[key] for key in allowed if key in record}
    clean.update(parsed)
    clean["record_id"] = str(clean.get("record_id") or uuid.uuid4().hex)[:128]
    clean["organization_key"] = _organization(clean.get("organization_key"))
    clean["created_at"] = _created_at(clean.get("created_at"))
    clean["actor_id"] = max(0, int(clean.get("actor_id") or 0))
    for key in ("actor_name", "salesperson", "account_scope", "task_type", "task_name", "task_id",
                "agent_job_id", "provider", "model", "request_id", "product_id", "status"):
        clean[key] = str(clean.get(key) or "")[:500]
    clean["actor_name"] = clean["actor_name"] or ("系统任务" if not clean["actor_id"] else "未提供姓名")
    clean["salesperson"] = clean["salesperson"] or clean["actor_name"]
    clean["task_type"] = clean["task_type"] or "other"
    clean["task_name"] = clean["task_name"] or TASK_TYPES.get(clean["task_type"], clean["task_type"])
    base_url = str(clean.pop("base_url", "") or "")
    clean["endpoint_host"] = _host(base_url) or str(clean.get("endpoint_host") or "")[:255]
    # A retry carries the original price snapshot. Direct records with no
    # snapshot are priced using the endpoint and current configured catalog.
    if not isinstance(clean.get("rate_metadata"), dict) or not clean.get("cost_status"):
        clean.update(estimate_cost(parsed, model=clean["model"], provider=clean["provider"],
                                   base_url=base_url or ("https://" + clean["endpoint_host"])))
    if not parsed["usage_consistent"]:
        clean.update(estimated_cost_cny=None, cost_known=False, cost_status="inconsistent_usage", rate_metadata={})
    return clean


def _record(payload):
    record = _normalize_record(payload)
    if record["usage_known"] and record["total_tokens"] == 0 and not any(
            record.get(key) for key in ("input_tokens", "output_tokens", "cached_tokens")):
        return {"skipped": True, "reason": "zero_tokens"}
    columns = ("record_id", "organization_key", "created_at", "actor_id", "actor_name", "salesperson",
               "task_type", "task_name", "task_id", "provider", "model", "request_id", "input_tokens",
               "output_tokens", "cached_tokens", "total_tokens", "usage_known", "estimated_cost_cny")
    db = _connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT id,payload_json FROM ai_usage_records WHERE organization_key=? AND "
                                  "(record_id=? OR (provider=? AND request_id=? AND request_id!='')) LIMIT 1",
                                  (record["organization_key"], record["record_id"], record["provider"], record["request_id"])).fetchone()
            if previous:
                original = json.loads(previous["payload_json"])
                merged = dict(original)
                for key in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
                    values = [value for value in (original.get(key), record.get(key)) if value is not None]
                    merged[key] = max(values) if values else None
                merged["usage_known"] = original.get("usage_known") or record.get("usage_known")
                merged.update(parse_usage(merged))
                # Provider polling can first return partial usage. Keep the
                # first identity/time and price version, then complete counts.
                if merged != original:
                    priced = estimate_cost(merged, model=original.get("model") or record["model"],
                                           base_url="https://" + (original.get("endpoint_host") or record["endpoint_host"]))
                    snapshot = original.get("rate_metadata") or record.get("rate_metadata")
                    if snapshot and merged.get("usage_consistent") and merged.get("input_tokens") is not None and merged.get("output_tokens") is not None:
                        cached_count = merged.get("cached_tokens") or 0
                        try:
                            amount = ((merged["input_tokens"] - cached_count) * Decimal(str(snapshot["input_rate"]))
                                      + cached_count * Decimal(str(snapshot["cached_input_rate"]))
                                      + merged["output_tokens"] * Decimal(str(snapshot["output_rate"]))) / Decimal(1000000)
                            if amount.is_finite() and amount >= 0:
                                priced = {"estimated_cost_cny": float(amount.quantize(Decimal("0.000000000001"))),
                                          "cost_known": True, "cost_status": "estimated", "rate_metadata": snapshot}
                        except (KeyError, ValueError, TypeError, InvalidOperation):
                            pass
                    merged.update(priced)
                    db.execute("UPDATE ai_usage_records SET input_tokens=?,output_tokens=?,cached_tokens=?,total_tokens=?,"
                               "usage_known=?,estimated_cost_cny=?,payload_json=? WHERE id=?",
                               [merged.get(key) for key in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens",
                                "usage_known", "estimated_cost_cny")] + [json.dumps(merged, ensure_ascii=False), previous["id"]])
                return {"record_id": original["record_id"], "duplicate": True, "record": merged}
            cursor = db.execute("INSERT OR IGNORE INTO ai_usage_records (" + ",".join(columns) + ",payload_json) VALUES ("
                                + ",".join("?" for _ in range(len(columns) + 1)) + ")",
                                [record.get(key) for key in columns] + [json.dumps(record, ensure_ascii=False)])
            if not cursor.rowcount:
                raise ValueError("AI 账单记录编号冲突")
        return {"record_id": record["record_id"], "duplicate": not bool(cursor.rowcount), "record": record}
    finally:
        db.close()


def _date_bound(value, *, end=False):
    if not value:
        return None
    try:
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CN_TIMEZONE)
        if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed += timedelta(days=1)
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except ValueError as exc:
        raise ValueError("AI 账单日期范围无效") from exc


def _scope_clauses(payload):
    if not payload.get("organization_key"):
        raise ValueError("AI 账单查询必须提供企业标识")
    clauses, params = ["organization_key = ?"], [_organization(payload.get("organization_key"))]
    ids = payload.get("scope_user_ids")
    if ids is not None:
        if not isinstance(ids, (list, tuple, set)):
            raise ValueError("AI 账单可见用户范围无效")
        ids = sorted(set(int(value) for value in ids))
        if any(value < 0 for value in ids):
            raise ValueError("AI 账单可见用户范围无效")
        clauses.append("actor_id IN (" + ",".join("?" for _ in ids) + ")" if ids else "0=1")
        params.extend(ids)
    return clauses, params


def _filter_clauses(payload):
    clauses, params = _scope_clauses(payload)
    start = _date_bound(payload.get("created_from") or payload.get("created_after") or
                        payload.get("date_from") or payload.get("start_date"))
    end = _date_bound(payload.get("created_before") or payload.get("date_to") or payload.get("end_date"), end=True)
    if start and end and start >= end:
        raise ValueError("AI 账单开始日期必须早于结束日期")
    for sql, value in (("created_at >= ?", start), ("created_at < ?", end)):
        if value:
            clauses.append(sql)
            params.append(value)
    for key in ("actor_id", "task_type", "model", "provider", "task_id"):
        value = payload.get(key)
        if value not in (None, ""):
            clauses.append(key + " = ?")
            params.append(int(value) if key == "actor_id" else str(value))
    salesperson = str(payload.get("salesperson") or "").strip()
    if salesperson:
        clauses.append("(instr(lower(actor_name), lower(?)) > 0 OR instr(lower(salesperson), lower(?)) > 0)")
        params.extend((salesperson, salesperson))
    keyword = str(payload.get("keyword") or payload.get("q") or "").strip()[:500]
    if keyword:
        columns = ("task_name", "task_type", "task_id", "actor_name", "salesperson", "model", "request_id")
        clauses.append("(" + " OR ".join("instr(lower(" + key + "), lower(?)) > 0" for key in columns) + ")")
        params.extend([keyword] * len(columns))
    return clauses, params


def _options(db, payload):
    clauses, params = _scope_clauses(payload)
    where = " WHERE " + " AND ".join(clauses)
    salespersons = [dict(row) for row in db.execute(
        "SELECT actor_id, MAX(actor_name) AS actor_name, MAX(salesperson) AS salesperson FROM ai_usage_records"
        + where + " GROUP BY actor_id ORDER BY actor_name", params)]
    tasks = [row[0] for row in db.execute("SELECT DISTINCT task_type FROM ai_usage_records" + where + " ORDER BY task_type", params)]
    return {"salespersons": salespersons,
            "task_types": [{"value": task, "label": TASK_TYPES.get(task, task)} for task in tasks],
            "models": [row[0] for row in db.execute("SELECT DISTINCT model FROM ai_usage_records" + where + " AND model != '' ORDER BY model", params)],
            "providers": [row[0] for row in db.execute("SELECT DISTINCT provider FROM ai_usage_records" + where + " AND provider != '' ORDER BY provider", params)]}


def _list(payload):
    clauses, params = _filter_clauses(payload)
    where = " WHERE " + " AND ".join(clauses)
    page = max(1, int(payload.get("page") or 1))
    page_size = max(1, min(200, int(payload.get("page_size") or payload.get("limit") or 30)))
    db = _connect()
    try:
        summary = dict(db.execute("""SELECT COUNT(*) AS record_count,
            COALESCE(SUM(CASE WHEN usage_known=1 THEN total_tokens END),0) AS total_tokens,
            COALESCE(SUM(CASE WHEN usage_known=1 THEN input_tokens END),0) AS input_tokens,
            COALESCE(SUM(CASE WHEN usage_known=1 THEN output_tokens END),0) AS output_tokens,
            COALESCE(SUM(CASE WHEN usage_known=1 THEN cached_tokens END),0) AS cached_tokens,
            COALESCE(SUM(estimated_cost_cny),0) AS estimated_cost_cny,
            COALESCE(SUM(estimated_cost_cny IS NOT NULL),0) AS estimated_record_count,
            COALESCE(SUM(estimated_cost_cny IS NULL),0) AS unpriced_record_count,
            COALESCE(SUM(usage_known=0 OR total_tokens IS NULL),0) AS unknown_usage_count,
            COUNT(DISTINCT actor_id) AS unique_actor_count FROM ai_usage_records""" + where, params).fetchone())
        rows = db.execute("SELECT id,payload_json FROM ai_usage_records" + where
                          + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", params + [page_size, (page-1)*page_size]).fetchall()
        items = [{**json.loads(row["payload_json"]), "id": row["id"]} for row in rows]
        return {"items": items, "total": summary["record_count"], "page": page, "page_size": page_size,
                "summary": summary, "options": _options(db, payload)}
    finally:
        db.close()


def ai_usage_billing(payload):
    """Local central-store dispatcher; callers must derive scope on the server."""
    payload = dict(payload or {})
    action = payload.get("action") or payload.get("method") or "list"
    if action == "record":
        return _record(payload.get("record") or payload.get("data") or payload)
    if action == "list":
        return _list(payload)
    if action == "options":
        db = _connect()
        try:
            return _options(db, payload)
        finally:
            db.close()
    raise ValueError("不支持的 AI 账单操作")


def _enqueue(record):
    db = _connect()
    try:
        with db:
            db.execute("INSERT OR IGNORE INTO ai_usage_outbox(record_id,payload_json,created_at) VALUES(?,?,?)",
                       (record["record_id"], json.dumps(record, ensure_ascii=False), record["created_at"]))
    finally:
        db.close()


def flush_outbox(*, limit=50, timeout=(3, 5), max_seconds=15):
    """Safely retry queued records: record_id makes unknown HTTP outcomes harmless."""
    if not _outbox_lock.acquire(blocking=False):
        return {"delivered": 0, "busy": True}
    try:
        from bit import bit_db_api
        db = _connect()
        try:
            rows = db.execute("SELECT record_id,payload_json FROM ai_usage_outbox ORDER BY created_at LIMIT ?",
                              (max(1, min(int(limit), 200)),)).fetchall()
            delivered = 0
            deadline = time.monotonic() + max(1, float(max_seconds))
            for row in rows:
                if time.monotonic() >= deadline:
                    break
                try:
                    if bit_db_api.DB_MODE == "mysql":
                        ai_usage_billing({"action": "record", "record": json.loads(row["payload_json"])})
                    else:
                        bit_db_api.ai_usage_billing({"action": "record", "record": json.loads(row["payload_json"]),
                                                    "_outbox_delivery": True, "_delivery_timeout": timeout})
                except Exception as exc:
                    # No provider content or credentials are retained in errors.
                    with db:
                        db.execute("UPDATE ai_usage_outbox SET attempts=attempts+1,last_attempt_at=?,last_error=? WHERE record_id=?",
                                   (_created_at(), type(exc).__name__, row["record_id"]))
                    break
                with db:
                    db.execute("DELETE FROM ai_usage_outbox WHERE record_id=?", (row["record_id"],))
                delivered += 1
            return {"delivered": delivered, "pending": db.execute("SELECT COUNT(*) FROM ai_usage_outbox").fetchone()[0]}
        finally:
            db.close()
    finally:
        _outbox_lock.release()


def _start_outbox_worker():
    global _outbox_worker
    with _worker_start_lock:
        if _outbox_worker is None or not _outbox_worker.is_alive():
            def work():
                while True:
                    _outbox_wake.wait(30)
                    _outbox_wake.clear()
                    try:
                        flush_outbox()
                    except Exception:
                        logging.getLogger(__name__).warning("AI usage outbox retry deferred", exc_info=True)
            _outbox_worker = threading.Thread(target=work, name="ai-usage-outbox", daemon=True)
            _outbox_worker.start()
    _outbox_wake.set()


def resume_outbox():
    """Restart delivery after a client process restart, even without new usage."""
    _start_outbox_worker()


def record_usage(response=None, *, usage=None, model="", provider="", base_url="", **metadata):
    """Record one actual model response without disrupting the business task."""
    try:
        raw = _mapping(response)
        parsed = parse_usage(response, usage=usage)
        if parsed["usage_known"] and parsed["total_tokens"] == 0 and not any(parsed.get(key) for key in ("input_tokens", "output_tokens", "cached_tokens")):
            return {"skipped": True, "reason": "zero_tokens"}
        inferred_type, inferred_name = _infer_task()
        context = {**current_usage_context(), **metadata}
        record = _normalize_record({**context, **parsed, "record_id": context.get("record_id") or uuid.uuid4().hex,
                                    "task_type": context.get("task_type") or inferred_type,
                                    "task_name": context.get("task_name") or TASK_TYPES.get(context.get("task_type"), inferred_name),
                                    "provider": provider or context.get("provider") or _host(base_url),
                                    "model": model or raw.get("model") or context.get("model") or "",
                                    "base_url": base_url or context.get("base_url") or "",
                                    "request_id": metadata.get("request_id") or raw.get("id") or raw.get("request_id") or ""})
        from bit import bit_db_api
        _enqueue(record)
        if bit_db_api.DB_MODE == "mysql":
            try:
                result = ai_usage_billing({"action": "record", "record": record})
                db = _connect()
                try:
                    with db:
                        db.execute("DELETE FROM ai_usage_outbox WHERE record_id=?", (record["record_id"],))
                finally:
                    db.close()
            except Exception:
                logging.getLogger(__name__).warning("AI usage ledger write deferred; durable event retained", exc_info=True)
                _start_outbox_worker()
                return {"record_id": record["record_id"], "queued": True}
            _start_outbox_worker()
            return result
        _start_outbox_worker()
        return {"record_id": record["record_id"], "queued": True}
    except Exception:
        logging.getLogger(__name__).exception("AI usage billing persistence failed")
        return {"recorded": False, "error": "billing_persistence_failed"}
