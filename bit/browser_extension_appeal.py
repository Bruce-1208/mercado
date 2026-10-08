"""Single-store appeal planning for a browser extension.

The browser owns Mercado navigation and submission. The server binds each run
to one authorized store, shares the Agent's phrases/AI copy and saves its normal
appeal records. No browser window or multi-store scheduler is started here.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


FORMS = ("延误", "侵权", "禁限售", "取消率", "投诉")
SITE_INFO = {
    "MLM": ("MX", "墨西哥"), "MLB": ("BR", "巴西"),
    "MLC": ("CL", "智利"), "MCO": ("CO", "哥伦比亚"),
    "MLA": ("AR", "阿根廷"), "MLU": ("UY", "乌拉圭"),
}
REPUTATION_FORMS = frozenset(("延误", "取消率", "投诉"))


class AppealError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def enabled(value):
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "0", "false", "no", "off")
    return bool(value)


def site_id(value):
    text = str(value or "").strip()
    for code, (short, name) in SITE_INFO.items():
        if text.upper() in (code, short, code + "-REMOTE") or text == name:
            return code
    raise AppealError("不支持的申诉站点")


def normalize_params(payload, identity):
    forms = payload.get("forms", payload.get("form", []))
    forms = [forms] if isinstance(forms, str) else list(forms or [])
    if not forms or any(value not in FORMS for value in forms):
        raise AppealError("请至少选择一个有效的任务类型")
    sites = payload.get("sites", [])
    sites = [sites] if isinstance(sites, str) else list(sites or [])
    selected = list(dict.fromkeys(site_id(value) for value in sites))
    allowed = {row["site_id"] for row in identity["sites"]}
    if not selected or set(selected) - allowed:
        raise AppealError("只能执行当前店铺已开启申诉的授权站点", 403)
    mode = str(payload.get("mode") or "人工客服").strip()
    copy_mode = str(payload.get("appeal_copy_mode") or "普通模式").strip()
    if mode not in ("AI客服", "人工客服"):
        raise AppealError("客服模式只支持 AI客服、人工客服")
    if copy_mode not in ("普通模式", "AI话术模式"):
        raise AppealError("不支持的申诉话术模式")
    if copy_mode == "AI话术模式" and mode != "AI客服":
        raise AppealError("AI话术模式只能配合 AI 客服使用")
    loop = str(payload.get("loop_count", 10)).strip().casefold()
    if loop in ("0", "permanent", "forever", "永久"):
        count = 0
    elif loop in ("1", "10", "20", "50"):
        count = int(loop)
    else:
        raise AppealError("循环次数只支持 1、10、20、50 或永久")
    message = str(payload.get("message") or "").strip()
    if len(message) > 10000:
        raise AppealError("自定义话术过长")
    return {"forms": [form for form in FORMS if form in forms],
            "sites": selected, "mode": mode, "appeal_copy_mode": copy_mode,
            "loop_count": count, "message": message}


def _identifiers(values, form, site):
    if not isinstance(values, list) or len(values) > 10000:
        raise AppealError("申诉编号列表格式无效")
    result = []
    for value in values:
        value = str(value or "").strip().upper()
        pattern = re.escape(site) + r"\d+" if form in ("侵权", "禁限售") else r"\d{6,32}"
        if not re.fullmatch(pattern, value):
            raise AppealError("申诉编号或所属站点无效")
        if value not in result:
            result.append(value)
    return result


def marketplace_accounts(token):
    from bit import mercado_infraction_sync as sync
    client, record = sync._client_and_token(token, timeout=15)
    try:
        return sync._marketplace_accounts(client, str(record["meli_user_id"]))
    except Exception as exc:
        if not sync._is_unauthorized_error(exc) or not record.get("refresh_token"):
            raise
        client, record = sync._client_and_token(sync._refresh_token(int(record["id"])), timeout=15)
        return sync._marketplace_accounts(client, str(record["meli_user_id"]))


def store_identifiers(identity, form, site):
    """Read one bound token/site, avoiding global plan/sync builders."""
    from bit import bit_db_api, bit_appeal_ai, mercado_infraction_sync as sync
    if form == "侵权":
        target = {"token_id": identity["token_id"], "name": identity["shop_name"],
                  "aliases": [identity["shop_name"]], "site_ids": [site]}
        data = sync.collect_live_detection_infractions([target], recent_days=100, max_workers=1)
        if data.get("failed_stores"):
            raise AppealError("当前店铺侵权 API 采集不完整，请重试", 503)
        return [row.get("编号") for row in data.get("data") or []
                if str(row.get("站点") or "").upper() == site
                and bit_appeal_ai._is_ai_infringement_record(row)
                and sync.is_auto_appeal_eligible_detection(row)
                and not sync.is_prohibited_detection(row)]
    if form == "禁限售":
        values, page = [], 1
        while True:
            data = bit_db_api.list_mercado_prohibited_listings(
                token_id=identity["token_id"], site_id=site,
                risk_type="prohibited", page=page, page_size=500) or {}
            values.extend(row.get("item_id") for row in data.get("rows") or []
                          if str(row.get("site_id") or "").upper() == site)
            if page >= max(1, int(data.get("pages") or 1)):
                return values
            page += 1
    raise AppealError("声誉订单需从当前浏览器店铺采集")


def product_contexts(identity, group, token_loader):
    from bit import mercado_infraction_sync as sync
    from bit.bit_ai_appeal_copy import MAX_DESCRIPTION_CHARS
    token = token_loader(identity["token_id"])
    client, token = sync._client_and_token(token, timeout=15)
    refreshed = False

    def read(path):
        nonlocal client, refreshed
        try:
            return client.request("GET", path, max_attempts=1)
        except Exception as exc:
            if refreshed or not sync._is_unauthorized_error(exc) or not token.get("refresh_token"):
                raise
            refreshed = True
            client, _ = sync._client_and_token(sync._refresh_token(identity["token_id"]), timeout=15)
            return client.request("GET", path, max_attempts=1)

    contexts = []
    for item_id in group["identifiers"]:
        try:
            item = read(f"/marketplace/items/{item_id}")
            seller = str(item.get("seller_id") or "")
            group_sellers = {identity["root_seller_id"], *(s["seller_id"] for s in identity["sites"] if s["site_id"] == group["site_id"])}
            if seller and seller not in group_sellers:
                raise ValueError("商品店铺不匹配")
            if item.get("site_id") and str(item["site_id"]).upper() != group["site_id"]:
                raise ValueError("商品站点不匹配")
            title = str(item.get("title") or "").strip()
            if not title:
                raise ValueError("商品标题为空")
            try:
                description = read(f"/items/{item_id}/description")
            except Exception as exc:
                if "失败 (404):" not in str(exc):
                    raise
                description = {}
            contexts.append({"product_id": item_id, "title": title,
                             "description": str(description.get("plain_text") or
                                                description.get("text") or "")[:MAX_DESCRIPTION_CHARS]})
        except Exception:
            raise AppealError("当前店铺商品资料读取失败，请检查店铺授权及商品状态", 503) from None
    return contexts


class AppealService:
    def __init__(self, *, scoped_tokens, token_loader, api_key, record_writer,
                 root=None, accounts_loader=marketplace_accounts,
                 identifiers_loader=store_identifiers, phrase_loader=None,
                 copy_generator=None, products_loader=None):
        self.scoped_tokens, self.token_loader = scoped_tokens, token_loader
        self.api_key, self.record_writer = api_key, record_writer
        self.accounts_loader, self.identifiers_loader = accounts_loader, identifiers_loader
        self.phrase_loader, self.copy_generator = phrase_loader, copy_generator
        self.products_loader = products_loader
        self.root = Path(root or os.environ.get("BIT_BROWSER_EXTENSION_APPEAL_ROOT") or
                         Path(__file__).resolve().parent / "runtime_locks" / "browser_extension_appeals")

    def context(self, user, payload):
        seller = str(payload.get("seller_id") or "").strip()
        if not re.fullmatch(r"\d+", seller):
            raise AppealError("无法读取当前美客多登录账号，请打开已登录的店铺后台")
        rows = [row for row in self.scoped_tokens(user) if enabled(row.get("enabled", True))]
        direct = [row for row in rows if str(row.get("meli_user_id") or "") == seller]
        candidates = direct or rows
        matches = []
        for row in candidates:
            token_id = int(row.get("id") or 0)
            if token_id <= 0:
                continue
            try:
                token = self.token_loader(token_id)
                if str(token.get("meli_user_id") or "") != str(row.get("meli_user_id") or ""):
                    raise ValueError("店铺授权账号已变更")
                accounts = self.accounts_loader(token)
            except Exception:
                if direct:
                    raise AppealError("当前店铺授权账号校验失败，请检查授权后重试", 503) from None
                continue
            root_seller = str(row.get("meli_user_id") or "")
            if seller != root_seller and seller not in {str(a.get("user_id")) for a in accounts}:
                continue
            allowed = {str(setting.get("site_id") or "").upper()
                       for setting in row.get("site_settings") or []
                       if enabled(setting.get("appeal_enabled"))}
            sites = [{"site_id": a["site_id"], "site_code": SITE_INFO[a["site_id"]][0],
                      "site_name": SITE_INFO[a["site_id"]][1], "seller_id": str(a["user_id"])}
                     for a in accounts if a.get("site_id") in SITE_INFO
                     and a["site_id"] in allowed
                     and (seller == root_seller or str(a["user_id"]) == seller)]
            if not sites:
                raise AppealError("当前店铺未开启该站点的进行申诉权限", 403)
            matches.append({"token_id": token_id, "seller_id": seller,
                            "root_seller_id": root_seller,
                            "organization_key": str(row.get("organization_key") or user.get("organization_key") or "wuhan-zeshun"),
                            "shop_name": str(row.get("display_name") or row.get("nickname") or ""),
                            "nickname": str(row.get("nickname") or ""), "sites": sites,
                            "allowed_seller_ids": sorted({root_seller, *(s["seller_id"] for s in sites)})})
        if len(matches) != 1:
            raise AppealError("当前美客多账号无法唯一匹配本人授权店铺", 403)
        return self._identity_snapshot(matches[0])

    @staticmethod
    def _identity_snapshot(identity):
        return {"identity": identity, "shop": {"id": identity["token_id"],
                "name": identity["shop_name"], "seller_id": identity["root_seller_id"]},
                "sites": identity["sites"], "seller_id": identity["seller_id"]}

    def _path(self, task_id):
        if not re.fullmatch(r"[a-f0-9]{32}", str(task_id or "")):
            raise AppealError("申诉任务编号无效")
        return self.root / f"{task_id}.json"

    def _load(self, user, task_id):
        path = self._path(task_id)
        if not path.is_file():
            raise AppealError("申诉任务不存在或已清理", 404)
        task = json.loads(path.read_text(encoding="utf-8"))
        if task["user_id"] != int(user.get("id") or 0):
            raise AppealError("只能操作本人创建的插件申诉任务", 403)
        identity = task["identity"]
        rows = [row for row in self.scoped_tokens(user)
                if int(row.get("id") or 0) == identity["token_id"]]
        if len(rows) != 1 or not enabled(rows[0].get("enabled", True)) or str(rows[0].get("meli_user_id") or "") != identity["root_seller_id"]:
            raise AppealError("任务绑定的店铺授权或账号权限已变更", 403)
        organization = str(rows[0].get("organization_key") or user.get("organization_key") or "wuhan-zeshun")
        if organization != identity["organization_key"]:
            raise AppealError("任务绑定店铺的企业归属已变更，请重新开始申诉", 403)
        allowed = {str(s.get("site_id") or "").upper() for s in rows[0].get("site_settings") or []
                   if enabled(s.get("appeal_enabled"))}
        if set(task["params"]["sites"]) - allowed:
            raise AppealError("任务绑定站点已关闭进行申诉权限", 403)
        return task

    def _write(self, task):
        path = self._path(task["task_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix("." + secrets.token_hex(8) + ".tmp")
        temporary.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)

    @contextmanager
    def _lock(self, task_id):
        from bit.bit_runtime_lock import InterProcessLock
        self._path(task_id)
        lease = InterProcessLock("browser_extension_appeal_" + task_id)
        if not lease.acquire(timeout=0):
            raise AppealError("申诉任务正在处理另一个请求，请稍后重试", 409)
        try:
            yield
        finally:
            lease.release()

    def plan(self, user, payload):
        if "context" in payload and not isinstance(payload["context"], dict):
            raise AppealError("当前店铺环境信息格式无效")
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            identity_payload = payload.get("context") if isinstance(payload.get("context"), dict) else payload
            identity = self.context(user, identity_payload)["identity"]
            params = normalize_params(payload, identity)
            if params["appeal_copy_mode"] == "AI话术模式" and set(params["forms"]) & {"侵权", "禁限售"} and not self.api_key(user):
                raise AppealError("请先在集成与凭证设置中配置阿里云百炼 API Key")
            task_id = secrets.token_hex(16)
            task = {"task_id": task_id, "user_id": int(user.get("id") or 0),
                    "identity": identity, "params": params, "status": "running",
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "rounds": {}}
        else:
            task = None
        with self._lock(task_id):
            task = task or self._load(user, task_id)
            if task["status"] != "running":
                raise AppealError("申诉任务已结束，不能继续执行", 409)
            if payload.get("expected_seller_id") and str(payload["expected_seller_id"]) != task["identity"]["seller_id"]:
                raise AppealError("当前店铺与读取时的店铺不一致，请重新读取", 409)
            updates = {key: payload[key] for key in task["params"] if key in payload}
            if "form" in payload and "forms" not in updates:
                updates["forms"] = payload["form"]
            if updates and normalize_params({**task["params"], **updates}, task["identity"]) != task["params"]:
                raise AppealError("任务参数已经绑定，修改后请重新开始申诉", 409)
            reported = payload.get("seller_id") or (payload.get("context") or {}).get("seller_id")
            if reported and str(reported) != task["identity"]["seller_id"]:
                raise AppealError("当前店铺与任务绑定店铺不一致", 409)
            try:
                number = int(payload.get("round_number", 1))
            except (TypeError, ValueError):
                raise AppealError("申诉轮次无效") from None
            limit = task["params"]["loop_count"]
            if number < 1 or (limit and number > limit) or number > len(task["rounds"]) + 1:
                raise AppealError("申诉轮次超出任务范围", 409)
            current = task["rounds"].get(str(number))
            if current is None:
                if number > 1:
                    previous = task["rounds"][str(number - 1)]
                    if any(not group.get("result") for group in previous["groups"]) or self._pending_collections(task, previous):
                        raise AppealError("上一轮尚未完成采集和结果记录", 409)
                current = {"identifiers": {}, "phrases": {}, "groups": []}
                task["rounds"][str(number)] = current
                for form in task["params"]["forms"]:
                    if task["params"]["mode"] == "人工客服" and (task["params"]["message"] or form == "投诉"):
                        for site in task["params"]["sites"]:
                            current["identifiers"][site + ":" + form] = []
                        continue
                    if form in REPUTATION_FORMS:
                        continue
                    for site in task["params"]["sites"]:
                        current["identifiers"][site + ":" + form] = _identifiers(
                            self.identifiers_loader(task["identity"], form, site), form, site)
            supplied = payload.get("reputation_identifiers") or {}
            if not isinstance(supplied, dict):
                raise AppealError("声誉订单采集结果格式无效")
            for raw_site, forms in supplied.items():
                site = site_id(raw_site)
                if site not in task["params"]["sites"] or not isinstance(forms, dict):
                    raise AppealError("订单采集结果包含未选择的店铺站点", 403)
                for form, values in forms.items():
                    if form not in REPUTATION_FORMS or form not in task["params"]["forms"]:
                        raise AppealError("订单采集结果包含未选择的任务类型")
                    key, ids = site + ":" + form, _identifiers(values, form, site)
                    if key in current["identifiers"] and current["identifiers"][key] != ids:
                        raise AppealError("本轮已签发的申诉编号不能修改", 409)
                    current["identifiers"][key] = ids
            self._build_groups(task, current, number)
            self._write(task)
            return self._snapshot(task, number)

    def _build_groups(self, task, current, number):
        from bit.bit_appeal_phrases import render_appeal_phrase, select_appeal_phrase
        previous = {group["group_id"]: group for group in current["groups"]}
        groups = []
        for form in task["params"]["forms"]:
            for site in task["params"]["sites"]:
                key = site + ":" + form
                values = current["identifiers"].get(key, [])
                manual = task["params"]["mode"] == "人工客服"
                ai = task["params"]["appeal_copy_mode"] == "AI话术模式" and form in ("侵权", "禁限售")
                size = 3 if ai else {"延误": 5, "投诉": 2}.get(form, 10)
                if manual:
                    if task["params"]["message"] or form == "投诉":
                        chunks = [[]]
                    elif values:
                        chunks = [random.sample(values, 10) if len(values) > 10 and form in ("侵权", "取消率") else values[:10]]
                    else:
                        chunks = []
                else:
                    chunks = [values[i:i + size] for i in range(0, len(values), size)]
                # Repeated prepare calls reuse the manual selection and message.
                if manual:
                    cached = [g for g in previous.values() if g["site_id"] == site and g["form"] == form]
                    if cached:
                        groups.extend(cached)
                        continue
                if chunks and not ai and not task["params"]["message"] and key not in current["phrases"]:
                    current["phrases"][key] = (self.phrase_loader or select_appeal_phrase)(form)
                for index, ids in enumerate(chunks, 1):
                    digest = hashlib.sha256(json.dumps([task["task_id"], number, site, form, ids]).encode()).hexdigest()[:32]
                    if digest in previous:
                        groups.append(previous[digest])
                        continue
                    ids_text = "、".join(ids)
                    message = "" if ai else (("" if manual else ids_text) + task["params"]["message"] if task["params"]["message"] else
                        render_appeal_phrase(current["phrases"][key], nickname=random.choice(("Bruce", "Jack", "Lucy", "James")),
                                             order_ids=ids_text, appeal_type=form))
                    groups.append({"group_id": digest, "round_number": number, "site_id": site,
                                   "site_code": SITE_INFO[site][0], "site_name": SITE_INFO[site][1],
                                   "form": form, "appeal_kind": form, "identifiers": ids,
                                   "group_index": index, "total_groups": len(chunks),
                                   "message": message, "needs_copy": ai,
                                   "appeal_copy_mode": "AI话术模式" if ai else "普通模式"})
        current["groups"] = groups

    @staticmethod
    def _pending_collections(task, current):
        return [{"site_id": site, "form": form} for form in task["params"]["forms"]
                if form in REPUTATION_FORMS for site in task["params"]["sites"]
                if site + ":" + form not in current["identifiers"]]

    def _snapshot(self, task, number=None):
        number = number or max((int(n) for n in task["rounds"]), default=1)
        current = task["rounds"].get(str(number), {"groups": [], "identifiers": {}})
        collections = self._pending_collections(task, current)
        return {**self._identity_snapshot(task["identity"]), "task_id": task["task_id"],
                "params": task["params"], "status": task["status"], "round_number": number,
                "created_at": task["created_at"], "finished_at": task.get("finished_at", ""), "error": task.get("error", ""),
                "round_interval_seconds": 60 if task["params"]["mode"] == "AI客服" else 600,
                "groups": [dict(group) for group in current["groups"] if not group.get("result")], "browser_collections": collections,
                "results_count": sum(sum(bool(g.get("result")) for g in r["groups"]) + len(r.get("empty_records", {}))
                                     for r in task["rounds"].values())}

    def _group(self, task, group_id):
        for current in task["rounds"].values():
            for group in current["groups"]:
                if group["group_id"] == group_id:
                    return group
        raise AppealError("申诉分组不存在或不属于本次任务", 404)

    def copy(self, user, payload):
        task_id = str(payload.get("task_id") or "")
        with self._lock(task_id):
            task = self._load(user, task_id)
            group = self._group(task, str(payload.get("group_id") or ""))
            if task["status"] != "running" or group.get("result"):
                raise AppealError("任务或申诉分组已结束", 409)
            if group["needs_copy"]:
                from bit.bit_ai_appeal_copy import generate_ai_appeal_copy
                key = self.api_key(user)
                if not key:
                    raise AppealError("请先配置阿里云百炼 API Key")
                loader = lambda ids: (self.products_loader(task["identity"], group) if self.products_loader else
                                      product_contexts(task["identity"], group, self.token_loader))
                from erp.ai_usage_billing import usage_context
                with usage_context(task_id=task_id, task_type="ai_appeal", task_name="泽顺插件自动化申诉"):
                    generated = (self.copy_generator or generate_ai_appeal_copy)(group["form"], group["identifiers"], key, product_loader=loader)
                group["message"], group["needs_copy"] = generated["message"], False
                self._write(task)
            return {"message": group["message"], "needs_copy": False}

    def result(self, user, payload):
        task_id = str(payload.get("task_id") or "")
        with self._lock(task_id):
            task = self._load(user, task_id)
            group = self._group(task, str(payload.get("group_id") or ""))
            if group.get("result"):
                return group["result"]
            if task["status"] != "running":
                raise AppealError("申诉任务已结束", 409)
            records = payload.get("log_records") or []
            if not isinstance(records, list) or len(records) > 1000 or len(json.dumps(records)) > 1000000:
                raise AppealError("申诉日志格式无效或过长")
            from bit import bit_appeal_ai as shared
            from bit.bit_appeal_state import result_from_logs, STATUS_LABELS
            safe_records = []
            for entry in records:
                if not isinstance(entry, dict):
                    raise AppealError("申诉日志格式无效")
                if entry.get("event") == "group_result":
                    continue
                if not isinstance(entry.get("extra") or {}, dict):
                    raise AppealError("申诉日志附加信息格式无效")
                extra = dict(entry.get("extra") or {})
                for key in ("infraction_ids", "prohibited_ids", "cancellation_ids", "complaint_order_ids", "delay_ids", "order_ids", "product_ids", "ids"):
                    extra.pop(key, None)
                extra["ids"] = group["identifiers"]
                safe_records.append({**entry, "window": task["identity"]["shop_name"],
                                     "site": group["site_name"], "extra": extra})
            error = str(payload.get("error") or "")[:10000]
            reported = payload.get("execution") or {}
            if reported:
                if not isinstance(reported, dict) or reported.get("status", reported.get("execution_status")) not in STATUS_LABELS:
                    raise AppealError("申诉执行结果无效")
                safe_records.append({"event": "group_result", "response": str(reported.get("response") or ""),
                                     "extra": {"result": {**reported, "status": reported.get("status") or reported["execution_status"]}}})
            fields = shared._collect_appeal_record_fields(safe_records)
            execution = result_from_logs(safe_records, error=error)
            summary = shared.summarize_ai_appeal_result(
                group["form"], group["identifiers"], fields["appeal_content"],
                fields["ai_replies"], force=True)
            record = {"event_id": group["group_id"], "appeal_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      "appeal_type": f"{group['form']}-第{group['group_index']}/{group['total_groups']}组",
                      "appeal_copy_mode": group["appeal_copy_mode"], "shop_name": task["identity"]["shop_name"],
                      "site": group["site_name"], "status": execution["message"],
                      "appeal_content": fields["appeal_content"], "identifiers": group["identifiers"],
                      "success_ids": summary["success_ids"], "failed_ids": summary["failed_ids"], "ai_replies": fields["ai_replies"],
                      "chat_history": fields["chat_history"], "ai_summary": f"第{group['group_index']}/{group['total_groups']}组：{summary['summary']}", "error": error,
                      "record_scope": "group", "group_index": group["group_index"], "total_groups": group["total_groups"],
                      "task_id": task_id, "round_number": group["round_number"], "token_id": task["identity"]["token_id"],
                      "organization_key": task["identity"]["organization_key"], "user_id": task["user_id"],
                      "seller_id": task["identity"]["seller_id"], "execution": execution,
                      "executor": {"runtime_role": "browser_extension", "execution_target": "browser_extension",
                                   "agent_name": "泽顺插件", "agent_id": "", "hostname": ""}}
            record_id = self.record_writer(record)
            group["result"] = {"record_id": record_id, "record": record}
            self._write(task)
            return group["result"]

    def finish(self, user, payload):
        task_id = str(payload.get("task_id") or "")
        with self._lock(task_id):
            task = self._load(user, task_id)
            status = str(payload.get("status") or "completed")
            if status not in ("completed", "stopped", "failed"):
                raise AppealError("任务结束状态无效")
            if status == "completed" and any(self._pending_collections(task, r) or any(not g.get("result") for g in r["groups"])
                                              for r in task["rounds"].values()):
                raise AppealError("申诉分组尚未完成采集和结果记录", 409)
            self._record_empty_steps(task)
            task.update(status=status, error=str(payload.get("error") or "")[:10000],
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self._write(task)
            return self._snapshot(task)

    def _record_empty_steps(self, task):
        from bit.bit_appeal_state import result_from_logs
        identity = task["identity"]
        for number, current in task["rounds"].items():
            saved = current.setdefault("empty_records", {})
            for key, identifiers in current["identifiers"].items():
                site, form = key.split(":", 1)
                if identifiers or key in saved or any(g["site_id"] == site and g["form"] == form for g in current["groups"]):
                    continue
                execution = result_from_logs([])
                event_id = hashlib.sha256(json.dumps([task["task_id"], number, site, form, "no_data"]).encode()).hexdigest()[:32]
                effective_copy = "AI话术模式" if task["params"]["appeal_copy_mode"] == "AI话术模式" and form in ("侵权", "禁限售") else "普通模式"
                record = {"event_id": event_id, "appeal_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          "appeal_type": form, "appeal_copy_mode": effective_copy, "shop_name": identity["shop_name"],
                          "site": SITE_INFO[site][1], "status": execution["message"], "appeal_content": "",
                          "identifiers": [], "success_ids": [], "failed_ids": [], "ai_replies": [], "chat_history": [],
                          "ai_summary": "当前店铺该站点没有可申诉编号。", "error": "", "record_scope": "site",
                          "task_id": task["task_id"], "round_number": int(number), "token_id": identity["token_id"],
                          "organization_key": identity["organization_key"], "seller_id": identity["seller_id"],
                          "user_id": task["user_id"], "execution": execution,
                          "executor": {"runtime_role": "browser_extension", "execution_target": "browser_extension",
                                       "agent_name": "泽顺插件", "agent_id": "", "hostname": ""}}
                saved[key] = self.record_writer(record)
                self._write(task)

    def status(self, user, task_id):
        return self._snapshot(self._load(user, task_id))
