"""Browser-extension execution workflow for AI 核重核价.

The extension owns the user's Edge tabs.  This module keeps the business data,
model calls and audit trail on the workbench server, while returning one small
browser action at a time to the extension.
"""

from __future__ import annotations

import hashlib
import time
import uuid

from erp.ai_weight_price.config import selection_params
from erp.ai_weight_price.image_first import highest_priced_variant
from erp.ai_weight_price.models import erp_value_equal, number, parse_weight_evidence
from erp.ai_weight_price.pricing import usd_cost


def _config(service, runtime_api_key=""):
    config = service.config.load()
    if runtime_api_key:
        config["_runtime_api_key"] = str(runtime_api_key).strip()
    return config


def _run(service):
    return service.store.state("run", {}) or {}


def _task(service, key):
    return service.store.get(str(key or "").strip())


def _client_config(service):
    config = service.config.load()
    selectors = dict(config.get("selectors") or {})
    selectors.update({
        "erp_edit_url": selectors.get("erp_edit_link", ""),
        "erp_detail": ".curd-detail-wrap",
        "erp_weight": selectors.get("erp_weight_input", ""),
        "erp_net_income": selectors.get("erp_net_income_input", ""),
        "erp_save": selectors.get("erp_save", ""),
    })
    return {
        "selectors": selectors,
        "max_candidates": int(config.get("max_candidates") or 10),
        "max_pages": int(config.get("max_pages") or 100),
        "workflow_mode": config.get("workflow_mode") or "image_first",
        "writeback_enabled": bool(config.get("writeback_enabled")),
    }


def _snapshot(service):
    data = service.status()
    run = _run(service)
    data["running"] = run.get("outcome") == "running" and not bool(
        service.store.state("stop_requested", False)
    )
    data["execution_target"] = "extension"
    data["execution_terminal"] = "浏览器插件"
    data["computer"] = "浏览器插件"
    data["client_config"] = _client_config(service)
    return data


def login_open(service):
    service.store.set_state("login", {"confirmed": False, "opened_at": time.time()})
    return {"url": "https://meli.zying.net/#/login", "data": _snapshot(service)}


def login_confirm(service, context):
    if not isinstance(context, dict) or not str(context.get("url") or "").strip():
        raise ValueError("未读取到智赢页面，请先打开并登录智赢")
    if not str(context.get("credential") or "").strip():
        raise ValueError("当前智赢页面尚未登录")
    categories = context.get("categories")
    if isinstance(categories, list) and categories:
        service.store.set_state("categories", categories)
        service.store.set_state("categories_meta", {
            "refreshed_at": time.time(),
            "source_url": str(context.get("url") or ""),
            "count": len(categories),
            "error": "",
        })
    credential_hash = hashlib.sha256(
        str(context.get("credential")).encode("utf-8", "replace")
    ).hexdigest()
    service.store.set_state("login", {
        "confirmed": True,
        "confirmed_at": time.time(),
        "source": "browser_extension",
    })
    service.store.set_state("login_browser", {
        "identity": "extension:" + credential_hash,
        "erp_list_url": str(context.get("url") or ""),
        "source": "browser_extension",
    })
    service.store.log("插件确认智赢已登录；后续页面操作由浏览器插件执行")
    return {"data": _snapshot(service)}


def categories(service, context):
    if not isinstance(context, dict):
        raise ValueError("未读取到智赢页面分类")
    options = context.get("categories")
    if not isinstance(options, list) or not options:
        raise ValueError("智赢未返回分类，请等待页面加载完成后重试")
    service.store.set_state("categories", options)
    service.store.set_state("categories_meta", {
        "refreshed_at": time.time(),
        "source_url": str(context.get("url") or ""),
        "count": len(options),
        "error": "",
    })
    return {"categories": options, "data": _snapshot(service)}


def _next_action(service):
    run = _run(service)
    task_ids = list(run.get("task_ids") or [])
    cursor = int(run.get("cursor") or 0)
    if service.store.state("stop_requested", False):
        run.update({"outcome": "stopped", "finished_at": time.time(), "message": "任务已停止"})
        service.store.set_state("run", run)
        service.store.save_run(run)
        return {"action": "done", "data": _snapshot(service)}
    if cursor >= len(task_ids) or int(run.get("processed_items") or 0) >= int(run.get("max_items") or len(task_ids) or 1):
        run.update({"outcome": "completed", "finished_at": time.time(), "message": "核重核价完成"})
        service.store.set_state("run", run)
        service.store.save_run(run)
        return {"action": "done", "data": _snapshot(service)}
    key = task_ids[cursor]
    run["current_task_id"] = key
    run["current_item_index"] = cursor + 1
    service.store.set_state("run", run)
    service.store.set_state("pipeline_current", {"task_id": key})
    return {"action": "search", "task": _task(service, key), "data": _snapshot(service)}


def start(service, payload):
    if not isinstance(payload, dict):
        raise ValueError("启动参数无效")
    if service.store.state("login", {}).get("confirmed") is not True:
        raise ValueError("请先在插件中登录智赢并确认登录")
    config = _config(service, payload.get("runtime_api_key"))
    selection = selection_params(payload.get("selection"), config)
    max_items = payload.get("max_items", 10)
    if type(max_items) is not int or not 1 <= max_items <= 10000:
        raise ValueError("本次商品数量必须是1–10000的整数")
    products = payload.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("插件没有读取到智赢商品，请先打开商品列表并重试")
    if selection.get("start_product_id"):
        wanted = str(selection["start_product_id"])
        positions = [i for i, item in enumerate(products)
                     if str((item or {}).get("erp_goods_id") or "") == wanted]
        if not positions:
            raise ValueError(f"当前智赢列表未找到起始产品编号 {wanted}")
        products = products[positions[0]:]
    products = products[:max_items]
    run_id = uuid.uuid4().hex
    task_ids = []
    with service.store.actor_scope(service.store.actor()):
        for index, item in enumerate(products, 1):
            if not isinstance(item, dict):
                continue
            key = str(item.get("erp_goods_id") or "").strip()
            title = str(item.get("title") or "").strip()
            image = str(item.get("main_image_url") or "").strip()
            if not key or not title or not image:
                continue
            record = {
                **item,
                "erp_goods_id": key,
                "title": title,
                "main_image_url": image,
                "source_index": index,
                "source_category": selection.get("category", ""),
                "source_category_label": item.get("zying_category_name") or "",
                "zying_category_name": item.get("zying_category_name") or "",
            }
            if not service.store.add(record):
                service.store.update(key, **record)
            task_ids.append(key)
    if not task_ids:
        raise ValueError("插件读取到的智赢商品缺少编号、标题或主图")
    run = {
        "run_id": run_id,
        "mode": "pipeline",
        "selection": selection,
        "max_items": len(task_ids),
        "task_ids": task_ids,
        "cursor": 0,
        "processed_items": 0,
        "current_item_index": 1,
        "success_items": 0,
        "skipped_items": 0,
        "blocked_items": 0,
        "risk_items": 0,
        "started_at": time.time(),
        "outcome": "running",
        "message": "插件正在执行浏览器操作",
    }
    service.store.set_state("run", run)
    service.store.set_state("run_selection", selection)
    service.store.set_state("stop_requested", False)
    service.store.set_state("circuit", None)
    service.store.set_state("run_error", None)
    service.store.save_run(run)
    for key in task_ids:
        service.store.record_run_item(run_id, key, execution_result="待处理", execution_reason="插件执行中")
    service.store.log(f"插件启动核重核价，共 {len(task_ids)} 件")
    return _next_action(service)


def _advance(service, status, reason):
    run = _run(service)
    if status == "success":
        run["success_items"] = int(run.get("success_items") or 0) + 1
    elif status == "blocked":
        run["blocked_items"] = int(run.get("blocked_items") or 0) + 1
    else:
        run["risk_items"] = int(run.get("risk_items") or 0) + 1
    run["processed_items"] = int(run.get("processed_items") or 0) + 1
    run["cursor"] = int(run.get("cursor") or 0) + 1
    run["message"] = reason
    service.store.set_state("run", run)
    service.store.save_run(run)
    return _next_action(service)


def search(service, payload):
    task = _task(service, payload.get("task_id"))
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("插件没有读取到1688以图搜货结果")
    config = _config(service, payload.get("runtime_api_key"))
    from erp.ai_weight_price.models import Models
    models = Models(config, service.store.log)
    approved, evidence = models.match_images(task, candidates[:int(config.get("max_candidates") or 10)])
    service.store.update(
        task["erp_goods_id"],
        image_match_evidence=evidence,
        match_evidence=evidence,
        candidate_count=len(candidates),
        image_match_confidence=max((item.get("review", {}).get("confidence", 0) for item in evidence), default=None),
    )
    if not approved:
        reason = f"前{len(candidates)}张候选图没有达到同款匹配门槛"
        service.store.skip(task["erp_goods_id"], reason)
        return _advance(service, "blocked", reason)
    candidate = approved[0]
    service.store.update(
        task["erp_goods_id"],
        best_match_url=candidate.get("url", ""),
        best_match_title=candidate.get("title", ""),
        best_match_image_url=candidate.get("main_image_url", ""),
        best_match_confidence=candidate.get("image_confidence"),
        best_match_approved=True,
    )
    return {"action": "detail", "task_id": task["erp_goods_id"], "candidate": candidate, "data": _snapshot(service)}


def detail(service, payload):
    task = _task(service, payload.get("task_id"))
    detail_data = payload.get("detail")
    if not isinstance(detail_data, dict):
        raise ValueError("插件没有读取到1688商品详情")
    selected = highest_priced_variant(detail_data.get("skus") or [])
    if not selected:
        reason = "1688详情没有可确认的最终变体价格"
        service.store.exception(task["erp_goods_id"], reason)
        return _advance(service, "risk", reason)
    raw_weight = selected.get("raw_weight") or detail_data.get("raw_weight") or ""
    if not raw_weight and detail_data.get("weight_g") not in (None, ""):
        raw_weight = f"{detail_data.get('weight_g')}g"
    weight = parse_weight_evidence(raw_weight)
    key = task["erp_goods_id"]
    task = service.store.update(
        key,
        stage="matched",
        supplier_url=detail_data.get("url") or payload.get("url") or "",
        supplier_sku_id=selected.get("id"),
        supplier_sku=selected.get("label") or "",
        merchant_id=detail_data.get("merchant_id"),
        match_confidence=task.get("best_match_confidence"),
        cost_price=selected.get("price"),
        weight_g=weight,
        supplier_price_evidence=selected,
        supplier_page_text=detail_data.get("description") or "",
        page_info_checked=True,
    )
    try:
        config = _config(service, payload.get("runtime_api_key"))
        pricing = usd_cost(selected["price"], service.exchange_rate(config))
    except Exception as exc:
        reason = "美元成本换算失败：" + str(exc)
        service.store.exception(key, reason)
        return _advance(service, "risk", reason)
    changes = {"net_income_usd": pricing["net_income_usd"]}
    if weight is not None:
        changes["weight_g"] = str(number(weight))
    reason = "图片匹配成功，已读取1688最高最终变体价格"
    task = service.store.update(key, net_income_usd=pricing["net_income_usd"], pricing=pricing,
                                decision_status="success", decision_reason=reason,
                                planned_changes={**changes, "review_status": "通过"})
    if not config.get("writeback_enabled"):
        dry = reason + "；测试模式未启用ERP回写，仅保留插件核重核价结论"
        service.store.update(key, status="risk", stage="done", decision_status="risk", decision_reason=dry,
                             saved_at=time.time(), write_verified=False)
        return _advance(service, "risk", dry)
    return {"action": "writeback", "task_id": key, "changes": {**changes, "review_status": "通过"},
            "data": _snapshot(service)}


def writeback(service, payload):
    task = _task(service, payload.get("task_id"))
    actual = payload.get("actual")
    changes = payload.get("changes")
    if not isinstance(actual, dict) or not isinstance(changes, dict):
        raise ValueError("插件未返回有效的智赢保存回读结果")
    for field, expected in changes.items():
        if field == "review_status":
            if str(actual.get(field) or "").replace(" ", "") != str(expected).replace(" ", ""):
                raise ValueError("智赢审核状态保存回读不一致")
        elif not erp_value_equal(field, actual.get(field), expected):
            raise ValueError(f"智赢保存回读不一致：{field}")
    now = time.time()
    service.store.update(task["erp_goods_id"], status="success", stage="done", saved_at=now,
                         erp_after=actual, write_verified=True, write_intent=changes,
                         decision_status="success", exception_reason="", exception_detail="")
    service.store.log("插件完成智赢回填并回读确认", task["erp_goods_id"])
    return _advance(service, "success", "当前商品已回填并确认，继续下一件")


def stop(service):
    service.store.set_state("stop_requested", True)
    service.store.log("插件已请求停止核重核价，保留当前进度")
    return {"data": _snapshot(service), "message": "已发送停止指令"}


def continue_run(service, payload):
    if payload.get("acknowledged") is not True:
        raise ValueError("请先完成登录或人机验证，并勾选继续原任务")
    service.store.set_state("stop_requested", False)
    service.store.set_state("circuit", None)
    run = _run(service)
    if run.get("outcome") == "blocked":
        run["outcome"] = "running"
        run["message"] = "插件继续执行浏览器操作"
        service.store.set_state("run", run)
        service.store.save_run(run)
    return _next_action(service)


def dispatch(service, action, payload):
    actions = {
        "login/open": lambda: login_open(service),
        "login/confirm": lambda: login_confirm(service, payload.get("context")),
        "login/supplier": lambda: {"url": "https://www.1688.com/", "data": _snapshot(service)},
        "categories/refresh": lambda: categories(service, payload.get("context")),
        "start": lambda: start(service, payload),
        "continue": lambda: continue_run(service, payload),
        "search": lambda: search(service, payload),
        "detail": lambda: detail(service, payload),
        "writeback": lambda: writeback(service, payload),
        "stop": lambda: stop(service),
    }
    if action == "login/open":
        return login_open(service)
    if action not in actions:
        raise ValueError("AI核重核价插件操作无效")
    return actions[action]()
