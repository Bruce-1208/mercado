"""Persistent all-category size chart uploads, with per-category feedback."""

import json
import os
import re
import uuid
from pathlib import Path

from bit.bit_runtime_lock import InterProcessLock
from bit.durable_tasks import DurableTaskStore
from mercado_api.client import MercadoAPIError


def error_message(exc):
    if isinstance(exc, (ValueError, MercadoAPIError)):
        detail = str(exc)
        detail = re.sub(r"(?i)(bearer\s+|access_token[=: ]+|refresh_token[=: ]+)\S+", r"\1[已隐藏]", detail)
        return detail[:2000]
    return f"服务异常（{type(exc).__name__}），请查看服务日志后重试"


def task_store():
    root = os.environ.get("BIT_SIZE_CHART_TASK_ROOT") or str(Path(__file__).resolve().parents[1] / ".data" / "size-chart-tasks")
    return DurableTaskStore(Path(root).expanduser().resolve())


def public_task(task):
    return {key: value for key, value in task.items() if key not in {"pid", "log_path", "queued_at", "worker_started_at", "can_stop"}}


def batch_templates(storage, org):
    from bit.size_charts import normalize_template
    library = json.loads(Path(__file__).with_name("size-chart-builtins.json").read_text(encoding="utf-8"))
    templates = storage.ensure_builtin_templates(org, [normalize_template(t) for t in library["templates"]],
                 [normalize_template(t) for t in library.get("previous_templates", [])])
    # Identical copies do not need separate work, but distinct local edits do.
    from erp.mercadolibre_size_chart_store import json_text
    chosen = {}
    for template in templates:
        chosen.setdefault(json_text(normalize_template(template)), template)
    return list(chosen.values())


def dispatch(action, payload, org, summaries, storage):
    from bit.size_charts import _positive_id
    tasks = task_store()
    if action == "batch-status":
        task_id = str(payload.get("task_id") or "")
        if task_id:
            task = tasks.get(task_id)
            if not task or task.get("organization_key") != org or not set(task.get("token_ids", [])) <= summaries.keys():
                raise ValueError("批量任务不存在或不在当前店铺授权范围")
            return {"task": public_task(task)}
        visible = [t for t in tasks.list("size_chart_batch") if t.get("organization_key") == org
                   and set(t.get("token_ids", [])) <= summaries.keys()]
        return {"task": public_task(visible[-1]) if visible else None}
    values = payload.get("token_ids")
    if not isinstance(values, list) or not values or len(values) > 200:
        raise ValueError("请选择上传店铺（最多 200 个）")
    token_ids = sorted({_positive_id(value, "店铺编号") for value in values})
    if not set(token_ids) <= summaries.keys():
        raise ValueError("目标店铺不在当前授权范围")
    if any(not summaries[token_id].get("enabled", True) for token_id in token_ids):
        raise ValueError("目标店铺已关闭，请重新选择")
    lock = InterProcessLock(f"size_chart_batch_start_{org}", owner="size_chart_batch")
    if not lock.acquire(timeout=0):
        raise ValueError("正在启动批量上传，请稍后刷新进度")
    try:
        for task in tasks.list("size_chart_batch"):
            if task.get("organization_key") == org and task.get("running") and set(token_ids) & set(task.get("token_ids", [])):
                raise ValueError("所选店铺已有批量上传任务，请查看进度")
        templates = batch_templates(storage, org)
        frozen = [{"id": t["id"], "revision": t["revision"], "name": t["name"]} for t in templates]
        state = {"task_id": uuid.uuid4().hex, "organization_key": org, "token_ids": token_ids,
                 "total": len(frozen) * len(token_ids), "completed": 0, "results": [],
                 "message": "正在准备全部分类，自动上传到店铺全部授权站点"}
        task = tasks.start("size_chart_batch", {"organization_key": org, "token_ids": token_ids,
                           "templates": frozen, "chart_db_path": str(storage.path)}, state)
        return {"task": public_task(task)}
    finally:
        lock.release()


def counts(results):
    return {"succeeded": sum(r["status"] == "succeeded" for r in results),
            "failed": sum(r["status"] in {"failed", "invalid"} for r in results),
            "skipped": sum(r["status"] == "duplicate" for r in results),
            "unknown": sum(r["status"] in {"unknown", "running"} for r in results)}


def run(payload, state, tasks, stop_event):
    from bit.size_charts import dispatch as chart_call
    from erp.mercadolibre_size_chart_store import SizeChartStore
    storage = SizeChartStore(payload["chart_db_path"])
    org, token_ids = payload["organization_key"], payload["token_ids"]
    results = []
    for template in payload["templates"]:
        for token_id in token_ids:
            if stop_event.is_set():
                return {"status": "stopped", "results": results, "completed": len(results), **counts(results)}
            label = {"template_id": template["id"], "template_name": template["name"], "token_id": token_id}
            tasks.update(state["task_id"], current=label, message=f"正在校验并上传 {template['name']} · 店铺 #{token_id}")
            try:
                preview = chart_call("preview", {"template_id": template["id"], "revision": template["revision"],
                                     "token_ids": [token_id]}, organization_key=org, allowed_token_ids=token_ids, store=storage)
                entry = preview["results"][0]
                if entry["status"] == "ready":
                    uploaded = chart_call("upload", {"preview_id": preview["preview_id"]},
                                          organization_key=org, allowed_token_ids=token_ids, store=storage)
                    entry = uploaded["results"][0]
                result = {**label, **{key: entry[key] for key in ("status", "sites", "message", "errors", "notices", "chart_id", "record_id") if key in entry}}
                if entry.get("record", {}).get("chart_id"):
                    result["chart_id"] = entry["record"]["chart_id"]
                if entry.get("record", {}).get("status") in {"unknown", "running"}:
                    result["status"] = entry["record"]["status"]
            except Exception as exc:
                import logging
                logging.getLogger(__name__).exception("尺码表批量上传分类失败")
                result = {**label, "status": "invalid", "message": error_message(exc)}
            results.append(result)
            tasks.update(state["task_id"], results=results, completed=len(results), **counts(results))
    summary = counts(results)
    return {"status": "partial" if summary["failed"] or summary["unknown"] else "success",
            "results": results, "completed": len(results), "current": None, **summary,
            "message": f"全部分类处理完成：成功 {summary['succeeded']}，已上传跳过 {summary['skipped']}，失败 {summary['failed']}，待核对 {summary['unknown']}"}
