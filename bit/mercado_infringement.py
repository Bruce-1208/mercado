"""Application service for list screening, scoped review and durable jobs."""

import json
from bit.knowledge_organization import bind as bind_knowledge_organization
import threading
from erp.ai_usage_billing import bind_usage
from datetime import datetime, timezone

from bit.infringement_state import canonical_record, content_hash, knowledge_version, result_state, review_is_current


LIST_FILTERS = {
    "search", "task_id", "source_type", "review_status", "publish_status", "weight_status",
    "weight_min", "weight_max", "price_min", "price_max", "net_proceeds_min", "net_proceeds_max",
    "date_from", "date_to", "management_category_id", "mercado_category", "zying_category",
    "product_developer_id", "collector_salesperson", "generator_salesperson", "salesperson",
}
RISK_STATUSES = {"unchecked", "queued", "checking", "low", "suspected", "high", "stale", "failed"}


def snapshot(row):
    return canonical_record(row)


def public_result(row, version=None, activity=None):
    data = snapshot(row)
    state = result_state(data, rule_version=version)
    database_complete = state == "completed"
    if state == "completed":
        state = {0: "low", 1: "suspected", 2: "high"}.get(data.get("risk_level"), "unchecked")
    if activity and activity.get("status") in {"queued", "processing", "failed", "interrupted", "cancelled"}:
        status = activity["status"]
        if status in {"queued", "processing"}:
            state = "queued" if status == "queued" else "checking"
        elif status == "failed" and not database_complete:
            state = "failed"
            data["error"] = activity.get("error", "检测失败，请重试")
        elif state == "unchecked":
            state = "unchecked"
    data["status"] = state
    data["review_current"] = review_is_current(data)
    data["manual_decision"] = data.get("manual_decision") or ""
    data["detection_scope"] = data.get("detection_scope") or "title"
    fields = {"status", "risk_level", "keywords", "reason", "checked_at", "check_status", "check_error", "error",
              "detection_scope", "content_hash", "rule_version", "model_version", "image_method", "image_checked",
              "manual_decision", "review_current", "review_reason", "reviewed_by", "reviewed_at", "review_expires_at",
              "review_content_hash", "review_site_ids", "review_evidence_ref", "review_history", "matched_rule_ids",
              "image_ocr_text", "title", "title_es", "title_pt", "product_category", "site_id", "category_id"}
    return {key: value for key, value in data.items() if key in fields}


class MercadoInfringementService:
    def __init__(self, store, dependencies):
        self.store, self.deps = store, dependencies
        self.threads = set()
        self.lock = threading.RLock()
        self.queue_slots = threading.BoundedSemaphore(2)

    @staticmethod
    def mode(value):
        if value not in {"collection", "products"}:
            raise ValueError("商品列表类型无效")
        return value

    @staticmethod
    def source(mode):
        return "collection_list" if mode == "collection" else "product_list"

    def knowledge(self):
        rows, after = [], None
        while True:
            data = self.deps["knowledge"](**({"after_id": after} if after is not None else {}), limit=5000) or {}
            rows.extend(data.get("rows") or [])
            if not data.get("has_more"):
                break
            next_after = int(data.get("next_after_id") or 0)
            if next_after <= (after or 0):
                raise ValueError("知识库分页游标无效")
            after = next_after
        return rows

    def read(self, mode, filters=None, *, limit=1000, offset=0, allowed_tokens=None):
        params = {key: value for key, value in (filters or {}).items() if key in LIST_FILTERS and value not in (None, "")}
        params.update(limit=limit, offset=offset)
        if mode == "collection":
            for key in ("source_type", "mercado_category", "zying_category", "product_developer_id", "collector_salesperson", "generator_salesperson"):
                params.pop(key, None)
            params["exclude_added"] = True
        else:
            params.pop("task_id", None)
            params.pop("salesperson", None)
            if allowed_tokens is not None:
                params["token_ids"] = sorted(allowed_tokens)
        return self.deps["list_" + mode](**params)

    def rows(self, mode, ids, allowed_tokens=None):
        if not isinstance(ids, list) or not ids or len(ids) > 50000:
            raise ValueError("请勾选 1–50000 件商品")
        try:
            normalized = list(dict.fromkeys(int(value) for value in ids))
        except (TypeError, ValueError):
            raise ValueError("商品编号无效") from None
        if any(value <= 0 for value in normalized):
            raise ValueError("商品编号无效")
        rows = self.deps["get_" + mode](normalized)
        if len(rows) != len(normalized) or {int(x["id"]) for x in rows} != set(normalized):
            raise ValueError("部分商品不存在，请刷新列表")
        if mode == "collection" and any(row.get("added_to_products") for row in rows):
            raise ValueError("部分商品已转入产品列表，请刷新后操作")
        if mode == "products" and allowed_tokens is not None:
            # Match the same SQL-backed visibility scope as the product list.
            visible = set()
            offset = 0
            while True:
                data = self.read(mode, limit=1000, offset=offset, allowed_tokens=allowed_tokens)
                page = data.get("rows") or []
                visible.update(int(x["id"]) for x in page if int(x["id"]) in normalized)
                offset += len(page)
                if not page or offset >= int(data.get("total") or len(page)) or visible == set(normalized):
                    break
            if visible != set(normalized):
                raise PermissionError("所选商品不在当前账号可访问范围内")
        return rows

    def decorate(self, rows, mode, user_id, knowledge=None):
        if knowledge is None and any(snapshot(row).get("content_hash") for row in rows):
            knowledge = self.knowledge()
        version = knowledge_version(knowledge) if knowledge is not None else None
        items = [{"source_type": self.source(mode), "row_id": int(row["id"])} for row in rows]
        activity = self.store.item_statuses(items, user_id) if user_id else {}
        return [{**row, "infringement": public_result(row, version, activity.get((self.source(mode), int(row["id"]))))} for row in rows]

    def filtered(self, mode, filters, user_id, allowed_tokens=None, *, limit=None, offset=0):
        filters = dict(filters or {})
        risk = str(filters.get("risk_status") or "")
        if risk and risk not in RISK_STATUSES:
            raise ValueError("侵权风险筛选值无效")
        knowledge = self.knowledge()
        rows, read_offset = [], 0
        while True:
            data = self.read(mode, filters, limit=1000, offset=read_offset, allowed_tokens=allowed_tokens)
            page = data.get("rows") or []
            rows.extend(page)
            read_offset += len(page)
            if int(data.get("total") or len(rows)) > 50000:
                raise ValueError("当前筛选超过 50000 件，请缩小检测或风险筛选范围")
            if not page or read_offset >= int(data.get("total") or len(rows)):
                break
        decorated = self.decorate(rows, mode, user_id, knowledge)
        if risk:
            decorated = [row for row in decorated if row["infringement"]["status"] == risk]
        if filters.get("risk_scope"):
            decorated = [row for row in decorated if row["infringement"]["detection_scope"] == filters["risk_scope"]]
        if filters.get("review_decision"):
            decision = {"released": "allow", "blocked": "block", "pending": ""}.get(filters["review_decision"])
            decorated = [row for row in decorated if row["infringement"].get("manual_decision", "") == decision]
        return {"rows": decorated[offset:offset + limit] if limit is not None else decorated,
                "total": len(decorated), "limit": limit, "offset": offset}

    def start(self, payload, user_id, allowed_tokens=None):
        mode = self.mode(payload.get("list_mode", "collection"))
        selection = payload.get("selection_mode", "selected")
        scope = payload.get("detection_scope", "title")
        if scope not in {"title", "title_image"} or selection not in {"selected", "filtered", "unchecked"}:
            raise ValueError("检测范围无效")
        if selection == "selected":
            rows = self.rows(mode, payload.get("item_ids"), allowed_tokens)
        else:
            rows = self.filtered(mode, payload.get("filters") or {}, user_id, allowed_tokens)["rows"]
            if selection == "unchecked":
                rows = [row for row in rows if row["infringement"]["status"] in {"unchecked", "failed", "stale"}]
        if not rows:
            raise ValueError("当前范围没有需要检测的商品")
        needs_ai = bool(payload.get("recheck")) or any(not snapshot(row).get("content_hash") for row in rows)
        if not needs_ai:
            current_version = knowledge_version(self.knowledge())
            needs_ai = any(result_state(snapshot(row), current_version, scope) != "completed" for row in rows)
        # Cached valid results do not need a credential or a second model call.
        if needs_ai and not self.deps["credential"](user_id):
            raise ValueError("请先在集成与凭证设置中配置阿里云百炼 Token")
        items = [{"source_type": self.source(mode), "row_id": int(row["id"])} for row in rows]
        params = {"list_mode": mode, "selection_mode": selection, "detection_scope": scope,
                  "recheck": bool(payload.get("recheck")), "allowed_tokens": sorted(allowed_tokens) if allowed_tokens is not None else None}
        task = self.store.create(user_id, params, items)
        try:
            thread = threading.Thread(target=bind_usage(bind_knowledge_organization(self.run),
                                      actor_id=user_id, task_id=task["task_id"],
                                      task_type="risk_check", task_name="美客多侵权检测"),
                                      args=(task["task_id"], user_id), daemon=True,
                                      name="mercado-infringement-" + task["task_id"][:8])
            with self.lock:
                self.threads.add(thread)
            thread.start()
        except Exception:
            self.store.set_status(task["task_id"], "failed", "后台任务启动失败，可重试")
            raise
        return self.store.get(task["task_id"], user_id)

    def run(self, task_id, user_id):
        credential = None
        def safe(value):
            if isinstance(value, str):
                return value.replace(credential, "[已隐藏凭证]") if credential else value
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            if isinstance(value, list):
                return [safe(item) for item in value]
            return value
        acquired = False
        try:
            while not acquired:
                acquired = self.queue_slots.acquire(timeout=.1)
                if self.store.cancelled(task_id):
                    self.store.set_status(task_id, "cancelled", "已取消，保留完成结果")
                    return
            task = self.store.get(task_id, user_id)
            params = task["params"]
            if self.store.cancelled(task_id):
                self.store.set_status(task_id, "cancelled", "已取消，保留完成结果")
                return
            self.store.set_status(task_id, "running", "正在检测商品")
            knowledge = self.knowledge()
            version = knowledge_version(knowledge)
            items = self.store.items(task_id)
            rows = self.rows(params["list_mode"], [item["row_id"] for item in items], params.get("allowed_tokens"))
            pending = []
            for row in rows:
                item = {"source_type": self.source(params["list_mode"]), "row_id": int(row["id"])}
                existing = snapshot(row)
                if not params["recheck"] and result_state(existing, version, params["detection_scope"]) == "completed":
                    self.store.update_item(task_id, {**existing, **item, "status": "completed", "reused": True})
                else:
                    pending.append(item)
            if pending:
                credential = self.deps["credential"](user_id)
                if not credential:
                    raise ValueError("当前账号的百炼 Token 已失效或被移除，请重新配置")
                result = self.deps["scan"](
                    items=pending, sources=[self.source(params["list_mode"])], hours=0, limit=0,
                    recheck=True, detection_scope=params["detection_scope"], knowledge_records=knowledge,
                    rule_version=version, api_key=credential, ai_provider="qianwen",
                    candidate_reader=self.deps["candidates"], risk_writer=self.deps["writer"],
                    stop_callback=lambda: self.store.cancelled(task_id),
                    item_callback=lambda event: self.store.update_item(task_id, safe(event)),
                    log_callback=lambda line: self.store.log(task_id, safe(line)))
                for record in result.get("results") or []:
                    self.store.update_item(task_id, safe({**record, "status": "completed"}))
            current = self.store.get(task_id, user_id)
            if self.store.cancelled(task_id):
                status = "cancelled"
            elif current["completed"] + current["failed"] != current["total"]:
                status = "partial" if current["completed"] else "failed"
            elif current["failed"]:
                status = "partial" if current["completed"] else "failed"
            else:
                status = "completed"
            self.store.set_status(task_id, status, f"已完成 {current['completed']} 件，失败 {current['failed']} 件")
        except Exception as exc:
            self.store.log(task_id, safe(str(exc)))
            self.store.set_status(task_id, "failed", "检测失败：" + safe(str(exc))[:500])
        finally:
            if acquired:
                self.queue_slots.release()
            with self.lock:
                self.threads.discard(threading.current_thread())

    def detail(self, mode, row_id, user_id, allowed_tokens=None):
        row = self.rows(self.mode(mode), [row_id], allowed_tokens)[0]
        knowledge = self.knowledge()
        decorated = self.decorate([row], mode, user_id, knowledge)[0]
        keywords = str(decorated["infringement"].get("keywords") or "").casefold()
        from bit.bit_infringement_knowledge import knowledge_rule_matches, knowledge_rule_names
        record = snapshot(row)
        matched_ids = {str(value) for value in record.get("matched_rule_ids") or []}
        if matched_ids:
            rules = [rule for rule in knowledge if str(rule.get("id")) in matched_ids]
        else:
            rules = [rule for rule in knowledge if knowledge_rule_matches(rule,
                site=str(record.get("site_id") or ""), category=str(record.get("category_id") or record.get("product_category") or ""),
                shop=str(record.get("token_id") or record.get("account_name") or ""))
                and any(name.casefold() in keywords for name in knowledge_rule_names(rule))]
        history = self.store.item_history(self.source(mode), row_id, user_id)
        if mode == "products" and row.get("collection_item_id"):
            history += self.store.item_history("collection_list", int(row["collection_item_id"]), user_id)
        return {"item": decorated, "infringement": decorated["infringement"], "rules": rules,
                "history": history + list(decorated["infringement"].get("review_history") or [])}

    def review(self, mode, row_id, payload, user_id, reviewer, allowed_tokens=None):
        self.rows(self.mode(mode), [row_id], allowed_tokens)
        decision = {"pending": "", "blocked": "block", "released": "allow"}.get(payload.get("decision"))
        if decision is None:
            raise ValueError("复核决定无效")
        reason = str(payload.get("reason") or "").strip()
        if not reason or len(reason) > 2000:
            raise ValueError("请填写 1–2000 字的复核依据")
        sites = payload.get("site_ids") or []
        if not isinstance(sites, list) or any(site not in {"MLM", "MLB", "MLA", "MLC", "MCO", "MLU", "MPE", "MEC"} for site in sites):
            raise ValueError("复核适用站点无效")
        evidence = str(payload.get("evidence_ref") or "").strip()[:2000]
        expires = payload.get("expires_at") or None
        if decision == "allow":
            if not sites or not evidence or not expires:
                raise ValueError("商品放行须填写站点、授权凭证和有效期")
            try:
                expiry = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    raise ValueError("商品放行有效期必须晚于当前时间")
            except (TypeError, ValueError):
                raise ValueError("商品放行有效期无效或已过期") from None
        result = self.deps["review"]([{ "source_type": self.source(mode), "row_id": row_id}],
            decision, reason, reviewer, expires_at=expires, site_ids=sites, evidence_ref=evidence,
            rule_version=knowledge_version(self.knowledge()))
        return {"review": result, **self.detail(mode, row_id, user_id, allowed_tokens)}
