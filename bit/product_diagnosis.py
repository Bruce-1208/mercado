"""Read-only Mercado diagnosis collection and durable background execution."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from erp.mercadolibre_diagnosis_rules import evaluate, plain, safe_link, score
from erp.mercadolibre_diagnosis_store import DiagnosisStore

LOCALES = {"MLM": "es_MX", "MLB": "pt_BR", "MLC": "es_CL", "MCO": "es_CO", "MLA": "es_AR", "MLU": "es_UY", "MPE": "es_PE"}
LOCAL_ITEM = re.compile(r"^(ML[A-Z]|MCO|MPE|MEC)\d+$")
USER_PRODUCT = re.compile(r"^(ML[A-Z]|MCO|MPE|MEC)U\d+$")
_guard = threading.Lock()
_wake = threading.Event()
_thread = None


def current_stores(allowed_token_ids=None):
    from bit import bit_mysql
    rows = (bit_mysql.list_mercado_store_tokens() or {}).get("rows") or []
    allowed = None if allowed_token_ids is None else {int(v) for v in allowed_token_ids}
    return {int(row["id"]): row for row in rows if row.get("id") and bool(row.get("enabled", True))
            and (allowed is None or int(row["id"]) in allowed)}


def _read(client, path, **kwargs):
    try:
        payload = client.request("GET", path, **kwargs)
        if not isinstance(payload, (dict, list)) or not payload:
            return {"status": "no_data", "message": "平台尚未提供此项数据"}
        return {"status": "ok", "data": payload}
    except Exception as exc:
        # Keep credentials/request URLs and exception bodies out of stored results.
        text = str(exc).lower()
        if "(403)" in text or "forbidden" in text:
            return {"status": "restricted", "message": "店铺模式或应用权限不支持此接口"}
        if "(404)" in text or "not found" in text or "not_found" in text:
            return {"status": "no_data", "message": "平台尚未生成此项数据，或商品不存在"}
        if "(400)" in text and ("cbt" in text or "not supported" in text):
            return {"status": "unsupported", "message": "此商品模型不支持该接口"}
        return {"status": "error", "message": "平台数据读取失败，请重新诊断；持续失败时检查授权"}


def collect_product(client, row, summary):
    item_id, site_id = str(row["item_id"]), str(row["site_id"])
    item = {"link_id": int(row["link_id"]), "token_id": int(row["token_id"]), "site_id": site_id,
            "item_id": item_id, "title": str(row.get("title") or ""), "seller_sku": str(row.get("seller_sku") or ""),
            "thumbnail_url": safe_link(row.get("thumbnail_url")), "permalink": safe_link(row.get("permalink")),
            "store_name": str(summary.get("display_name") or summary.get("nickname") or row["token_id"])}
    sources = {}
    if not LOCAL_ITEM.fullmatch(item_id) or not item_id.startswith(site_id):
        sources = {key: {"status": "unsupported", "message": "请先同步本地站点商品链接"} for key in ("item", "quality", "experience")}
        return evaluate(item, sources)
    sources["item"] = _read(client, f"/marketplace/items/{item_id}", params={"attributes": "id,site_id,title,status,available_quantity,cbt_item_id,user_product_id,permalink,secure_thumbnail"})
    live = sources["item"].get("data") or {}
    if not isinstance(live, dict) or (live and str(live.get("id") or "") != item_id):
        sources["item"] = {"status": "error", "message": "平台返回的商品与目标商品不一致"}
        live = {}
    if sources["item"]["status"] == "ok" and not live.get("status"):
        sources["item"] = {"status": "no_data", "message": "平台未返回商品状态"}
    elif sources["item"]["status"] == "ok" and (type(live.get("available_quantity")) is not int or live["available_quantity"] < 0):
        sources["item"] = {"status": "partial", "data": live, "message": "平台未返回有效可售库存，商品状态已读取"}
    for field in ("title", "permalink"):
        if live.get(field):
            item[field] = safe_link(live[field]) if field == "permalink" else str(live[field])
    if live.get("secure_thumbnail"):
        item["thumbnail_url"] = safe_link(live["secure_thumbnail"])
    item["listing_status"] = live.get("status") or ""
    item["available_quantity"] = live.get("available_quantity")
    sources["quality"] = _read(client, f"/item/{item_id}/performance")
    q = sources["quality"].get("data")
    if sources["quality"]["status"] == "ok" and (not isinstance(q, dict) or score(q.get("score")) is None):
        sources["quality"] = {"status": "no_data", "message": "平台尚未提供有效刊登质量分"}
    up_id = str(live.get("user_product_id") or "")
    try:
        previous = json.loads(row.get("remote_json") or "{}")
    except (ValueError, TypeError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    # Current platform identity wins. Stored CBT mapping is only a fallback when
    # the current item did not provide one; it cannot override an explicit UP ID.
    parent = str(live.get("cbt_item_id") or previous.get("cbt_item_id") or "")
    if USER_PRODUCT.fullmatch(up_id) and up_id.startswith(site_id) and site_id in LOCALES:
        sources["experience"] = _read(client, f"/marketplace/user_products/{up_id}/purchase_experience", params={"locale": LOCALES[site_id]})
        item["user_product_id"] = up_id
    elif re.fullmatch(r"CBT\d+", parent):
        sources["experience"] = _read(client, f"/marketplace/items/{parent}/purchase_experience")
    else:
        sources["experience"] = {"status": "no_data", "message": "缺少 CBT 父商品或 User Product 映射，请先同步店铺链接"}
    experience = sources["experience"]
    exp = experience.get("data")
    if experience.get("status") == "ok":
        if isinstance(exp, list):
            exp = next((v for v in exp if isinstance(v, dict) and str(v.get("item_id") or "") == item_id), None)
        if not isinstance(exp, dict) or (exp.get("up_id") and exp["up_id"] != up_id) or (exp.get("item_id") and exp["item_id"] != item_id):
            sources["experience"] = {"status": "no_data", "message": "平台未返回当前站点商品的购物体验"}
        elif score((exp.get("reputation") or {}).get("value")) is None:
            sources["experience"] = {"status": "no_data", "message": "平台购物体验样本不足，暂不能评分"}
        else:
            experience["data"] = exp
    return evaluate(item, sources)


def load_links(token_id, site_id="", after_id=0):
    from bit.clearance_sources import _connect
    while True:
        connection = _connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id AS link_id,token_id,site_id,item_id,title,thumbnail_url,permalink,seller_sku,remote_json "
                               "FROM erp_mercadolibre_store_links WHERE token_id=%s AND is_current=1 AND id>%s" +
                               (" AND site_id=%s" if site_id else "") + " ORDER BY id LIMIT 200",
                               (token_id, after_id, *([site_id] if site_id else [])))
                rows = [dict(v) for v in cursor.fetchall()]
        finally:
            connection.close()
        if not rows:
            return
        next_id = int(rows[-1]["link_id"])
        if next_id <= after_id:
            raise RuntimeError("商品列表分页失败")
        yield from rows
        after_id = next_id


def run_scan(run, store):
    from bit.clearance_sources import _client_for_store
    from bit.sync_capacity import run_store
    scope_errors = list(run.get("scope_errors") or [])
    try:
        for scope in run["scopes"]:
            token_id = int(scope["token_id"])
            if token_id < run["cursor_token"]:
                continue
            try:
                summary = current_stores({token_id}).get(token_id)
                if not summary:
                    raise ValueError("店铺授权已停用或移除")
                client, _ = _client_for_store(token_id)
                after_id = run["cursor_link"] if token_id == run["cursor_token"] else 0
                for row in load_links(token_id, scope.get("site_id", ""), after_id):
                    result = run_store("product-diagnosis", token_id, lambda: collect_product(client, row, summary))
                    store.save(result, run["id"])
                store.finish_scope(run["id"], token_id, scope.get("site_id", ""))
            except Exception:
                logging.exception("商品诊断店铺读取失败：%s", token_id)
                scope_errors.append({"token_id": token_id, "site_id": scope.get("site_id", ""),
                                     "message": "店铺读取未完成，请核对授权和商品同步后重试；保留已有结果"})
                store.update_run(run["id"], scope_errors=scope_errors)
            store.update_run(run["id"], cursor_token=token_id + 1, cursor_link=0)
        # Products lacking one source remain visible with the partial-data label.
        saved = next(v for v in store.runs({int(s["token_id"]) for s in run["scopes"]}) if v["id"] == run["id"])
        incomplete = bool(saved["failed"] or scope_errors)
        status = "failed" if scope_errors and not saved["processed"] else "partial" if incomplete else "succeeded"
        message = f"{len(scope_errors)} 个店铺读取未完成，请核对后重试；已有结果保留" if scope_errors else "部分平台数据待核对" if incomplete else "诊断完成"
        store.update_run(run["id"], status=status, total=saved["processed"], message=message)
    except Exception:
        logging.exception("商品诊断任务失败：%s", run["id"])
        store.update_run(run["id"], status="failed", message="诊断未完成，请检查授权及商品同步；已完成的结果仍可查看")


def _worker():
    from bit.bit_runtime_lock import InterProcessLock
    while True:
        lock = InterProcessLock("product_diagnosis", owner="商品经营诊断")
        try:
            if lock.acquire():
                try:
                    store = DiagnosisStore()
                    run = store.next_run()
                    if run:
                        run_scan(run, store)
                finally:
                    lock.release()
        except Exception:
            logging.exception("商品诊断后台读取失败")
        _wake.wait(10)
        _wake.clear()


def ensure_worker():
    global _thread
    from bit.service_topology import owns_background
    from bit import bit_db_api
    if bit_db_api.DB_MODE != "mysql" or not owns_background("stores"):
        return
    with _guard:
        if not _thread or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="product-diagnosis", daemon=True)
            _thread.start()
    _wake.set()


def add_today_task(row, summary, actor):
    from bit import bit_db_api
    if not row["issues"] or row.get("stale"):
        raise ValueError("请先诊断并确认当前问题，再加入今日必办")
    organization = str(summary.get("organization_key") or "")
    if not organization:
        raise ValueError("店铺缺少企业归属，请先完善授权设置")
    fingerprint = sorted((v["key"], v["evidence"]) for v in row["issues"])
    key = hashlib.sha256(json.dumps(["product_diagnosis", organization, row["token_id"], row["item_id"], fingerprint], ensure_ascii=False).encode()).hexdigest()
    first = row["issues"][0]
    return bit_db_api.upsert_mercado_operator_task({
        "task_key": key, "organization_key": organization, "token_id": row["token_id"],
        "source": "product_diagnosis", "topic": "product_diagnosis", "resource": row["item_id"],
        "title": f"商品诊断：{first['title']} · {row['item_id']}",
        "details": f"由 {actor} 加入；商品：{row['title']}。" + "；".join(v["title"] + "：" + v["suggestion"] for v in row["issues"]) +
                   "。24 小时为内部建议处理时限，非平台截止时间。诊断时间：" + row["checked_at"],
        "entry_url": f"/product-diagnosis?link_id={row['link_id']}",
        "priority": "urgent" if row["risk"] == "high" else "high" if row["risk"] == "medium" else "normal",
        "due_at": (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
    })


def dispatch(operation, payload, *, allowed_token_ids=None, actor=""):
    stores = current_stores(allowed_token_ids)
    store = DiagnosisStore()
    selected = payload.get("token_ids") or []
    if not isinstance(selected, list) or any(type(v) is not int or v <= 0 for v in selected):
        raise ValueError("店铺编号无效")
    if not set(selected) <= stores.keys():
        raise PermissionError("当前账号无权访问所选店铺，或店铺已停用")
    ids = sorted(selected or stores.keys())
    if operation == "list":
        result = store.list_page(ids, **{key: payload[key] for key in ("site_id", "search", "risk", "category", "page", "page_size") if key in payload})
        for row in result["rows"]:
            settings = next((v for v in stores[row["token_id"]].get("site_settings") or [] if v.get("site_id") == row["site_id"]), {})
            row.update(salesperson=settings.get("salesperson", ""), group_name=settings.get("group_name", ""))
        result["stores"] = [{"id": key, "name": str(v.get("display_name") or v.get("nickname") or key)} for key, v in stores.items()]
        result["runs"] = store.runs(ids)
        return result
    if operation == "start":
        if not ids:
            raise ValueError("暂无可诊断的已授权店铺")
        run_id = store.create_run([{"token_id": key, "site_id": payload.get("site_id", "")} for key in ids], actor)
        ensure_worker()
        return {"run_id": run_id}
    if operation in {"detail", "today"}:
        row = store.get(int(payload["link_id"]), ids)
        if operation == "today":
            return {"task": add_today_task(row, stores[row["token_id"]], actor)}
        return row
    raise ValueError("商品诊断操作无效")
