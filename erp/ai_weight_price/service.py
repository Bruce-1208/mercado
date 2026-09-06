import hashlib
import os
import random
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from bit.bit_runtime_lock import InterProcessLock
from .browser import Browser, CircuitOpen, Stopped
from .config import Config, selection_key, selection_params
from .edge import debugger_identity, open_edge
from .models import Models, number, validate_weight
from .store import CHINA, Store


class Service:
    def __init__(self, root, browser_factory=Browser, models_factory=Models):
        self.store = Store(root)
        self.config = Config(root)
        self.browser_factory, self.models_factory = browser_factory, models_factory
        self.stop_event = threading.Event()
        self.thread = None
        self.guard = threading.RLock()
        self.lock_key = "ai_weight_price_" + hashlib.sha256(str(self.store.root.resolve()).encode()).hexdigest()[:16]

    def lock(self):
        return InterProcessLock(self.lock_key, owner="AI核重核价")

    @contextmanager
    def idle(self):
        with self.guard:
            lock = self.lock()
            if not lock.acquire():
                raise ValueError("任务运行中，请先停止再修改配置或任务")
            try:
                yield
            finally:
                lock.release()

    def status(self):
        lock = self.lock()
        owner = lock.read_owner()
        running = bool(owner and not lock._is_stale())
        return {"running": running, "counts": self.store.counts(), "quota": self.store.quota(),
                "circuit": self.store.state("circuit"), "run": self.store.state("run", {}),
                "storage": str(self.store.root), "collection": self.store.state("collection"),
                "login": self.store.state("login", {"confirmed": False}),
                "selection": self.store.state("run_selection")}

    def open_login(self):
        with self.idle():
            self.store.set_state("login", {"confirmed": False, "opened_at": time.time()})
            config = self.config.load()
            # Migrate older installations from the general Zying console to the
            # Mercado-specific product console.
            if config["erp_list_url"] != "https://meli.zying.net/#/product":
                config["erp_list_url"] = "https://meli.zying.net/#/product"
                self.config.save(config)
            open_edge(config["cdp_url"], self.store.root)
            try:
                with self.browser_factory(config, threading.Event(), self.store.log) as browser:
                    browser.open_login()
            except Exception as exc:
                raise ValueError(f"无法打开 Edge 登录页面：{exc}") from exc
            self.store.log("已打开 Edge 智赢登录窗口，等待人工输入账号密码并在控制台确认")

    def confirm_login(self):
        with self.idle():
            config = self.config.load()
            identity = debugger_identity(config["cdp_url"])
            if not identity:
                raise ValueError("尚未连接到 Edge，请先点击“打开 Edge 登录智赢”")
            try:
                with self.browser_factory(config, threading.Event(), self.store.log) as browser:
                    detail = browser.confirm_login()
            except Exception as exc:
                self.store.set_state("login", {"confirmed": False})
                raise ValueError(str(exc)) from exc
            self.store.set_state("login", {"confirmed": True, "confirmed_at": detail["confirmed_at"]})
            self.store.set_state("login_browser", {"identity": identity, "cdp_url": config["cdp_url"], "erp_list_url": config["erp_list_url"]})
            self.store.log("操作者确认已登录，Edge中的智赢后台登录状态检查通过")
        return self.store.state("login")

    def require_login(self, config):
        state = self.store.state("login", {})
        binding = self.store.state("login_browser", {})
        if not state.get("confirmed"):
            raise ValueError("请先在 Edge 登录智赢，然后点击“我已成功登录”")
        if (binding.get("cdp_url") != config["cdp_url"] or binding.get("erp_list_url") != config["erp_list_url"]
                or not binding.get("identity") or debugger_identity(config["cdp_url"]) != binding["identity"]):
            self.store.set_state("login", {"confirmed": False})
            raise ValueError("Edge会话已关闭、更换或连接配置已变更，请重新打开并确认登录")

    def categories(self):
        with self.idle():
            config = self.config.load()
            self.require_login(config)
            try:
                with self.browser_factory(config, threading.Event(), self.store.log) as browser:
                    browser.confirm_login()
                    options = browser.categories()
            except Exception as exc:
                self.store.log(f"分类刷新失败，保留上次分类：{exc}", level="ERROR")
                self.store.set_state("categories_meta", {**self.store.state("categories_meta", {}), "error": str(exc)})
                raise ValueError(f"读取分类失败：{exc}") from exc
            if not options:
                raise ValueError("智赢未返回分类，未更新分类列表，请稍后重试")
            self.store.set_state("categories", options)
            self.store.set_state("categories_meta", {"refreshed_at": time.time(), "source_url": config["erp_list_url"],
                                                       "count": len(options), "error": ""})
            return options

    def preflight(self, config, mode, task_id=None):
        task = self.store.get(task_id) if task_id else {}
        required = ["erp_rows", "erp_id", "erp_title", "erp_image", "erp_next", "erp_category_control", "erp_page_active", "erp_page_first"] if mode == "collect" else []
        if mode == "process":
            if not task.get("supplier_sku_id"):
                required += ["search_input", "search_button", "result_links", "supplier_title", "supplier_image",
                             "supplier_merchant", "supplier_merchant_attribute", "sku_rows", "sku_id_attribute", "sku_label", "sku_price"]
            if not task.get("weight_g"):
                required += ["chat_identity", "chat_identity_attribute", "chat_messages", "chat_message_id_attribute", "chat_message_time_attribute"]
                if not task.get("conversation_url"):
                    required += ["chat_open", "chat_input", "chat_send"]
        if mode == "process" and config["writeback_enabled"]:
            required += ["erp_edit_id", "erp_edit_sku", "erp_cost_input", "erp_weight_input", "erp_save", "erp_saved"]
        missing = [key for key in required if not config["selectors"][key]]
        if missing:
            raise ValueError("首次运行需要完成页面适配，缺少DOM字段：" + "、".join(missing))
        if mode == "process" and not task.get("weight_g") and urlsplit(config["api_base_url"]).hostname not in ("localhost", "127.0.0.1", "::1") and not os.environ.get(config["api_key_env"]):
            raise ValueError("请在本机环境变量 " + config["api_key_env"] + " 中设置模型密钥")

    def start(self, mode="process", task_id=None, selection=None):
        if mode not in ("collect", "process", "probe"):
            raise ValueError("运行模式无效")
        with self.guard:
            config = self.config.load()
            if mode != "probe":
                self.require_login(config)
                if not task_id:
                    selection = selection_params(selection if selection is not None else self.store.state("run_selection"), config)
                    if selection["category"] and not any(c["value"] == selection["category"] for c in self.store.state("categories", [])):
                        raise ValueError("请选择已读取的智赢分类，或留空表示不筛选分类")
                    config["run_selection"] = selection
                    config["run_scope"] = selection_key(selection, config)
                    if mode == "process" and not self.store.list(scope=config["run_scope"])["total"]:
                        raise ValueError("所选分类及页码范围尚无采集任务，请先点击“采集所选范围”")
                self.preflight(config, mode, task_id)
            lock = self.lock()
            if not lock.acquire():
                raise ValueError("已有进程正在运行此模块")
            self.stop_event.clear()
            self.store.set_state("stop_requested", False)
            self.store.set_state("run_error", None)
            if selection:
                self.store.set_state("run_selection", selection)
            self.store.set_state("run", {"mode": mode, "selection": selection, "started_at": time.time(), "message": "正在连接本机Edge"})
            self.thread = threading.Thread(target=self.run, args=(config, mode, task_id, lock), daemon=True)
            try:
                self.thread.start()
            except Exception:
                lock.release()
                raise

    def stop(self):
        self.stop_event.set()
        self.store.set_state("stop_requested", True)
        self.store.log("已请求停止；保留进度及商家去重记录")

    def circuit(self, error):
        self.store.set_state("circuit", {"reason": str(error), "at": time.time()})
        self.store.log("聊天熔断，禁止新咨询：" + str(error), level="CRITICAL")

    def run(self, config, mode, task_id, lock):
        try:
            self.store.recover()
            with self.browser_factory(config, self.stop_event, self.store.log) as browser:
                if mode == "probe":
                    self.store.log("Edge连接检查通过；未采集、发消息或回写")
                    return
                browser.confirm_login()
                if mode == "collect":
                    browser.collect(self.store)
                    return
                models = self.models_factory(config, self.store.log)
                while not self.stop_event.is_set() and not self.store.state("stop_requested", False):
                    if self.store.state("circuit"):
                        self.store.log("存在持久化熔断，需人工处理页面后解除", level="WARNING")
                        break
                    self.tick(browser, models, config, task_id)
                    self.store.export()
                    if task_id:
                        if self.store.get(task_id)["status"] in ("success", "exception"):
                            break
                    elif not any(self.store.counts(config.get("run_scope"))[s] for s in ("pending", "waiting_merchant_reply")):
                        break
                    if self.stop_event.wait(1):
                        break
        except CircuitOpen as exc:
            self.circuit(exc)
        except Stopped:
            self.store.log("运行已停止，进度已保存")
        except Exception as exc:
            self.store.log(f"运行停止：{type(exc).__name__}: {exc}", level="ERROR")
            self.store.set_state("run_error", str(exc))
        finally:
            self.store.set_state("run", {"mode": mode, "finished_at": time.time(), "message": "已停止"})
            try:
                self.store.export()
            finally:
                lock.release()

    def tick(self, browser, models, config, task_id=None):
        now = time.time()
        # Waiting conversations take priority; deadline checks are independent of polling interval.
        for task in self.store.list("waiting_merchant_reply", page_size=10000, scope=config.get("run_scope"))["rows"]:
            if task_id and task["erp_goods_id"] != task_id:
                continue
            if now >= min(task["next_poll_at"], task["deadline"]):
                self.poll(task, browser, models, config, now)
        if self.stop_event.is_set() or self.store.state("circuit"):
            return
        # Page through the queue so a deferred first page cannot starve later tasks.
        page = 1
        while True:
            tasks = self.store.list("pending", page=page, page_size=100, scope=config.get("run_scope"))["rows"]
            if not tasks:
                return
            for task in tasks:
                if task_id and task["erp_goods_id"] != task_id:
                    continue
                if task.get("next_attempt_at", 0) <= now:
                    self.process(task, browser, models, config)
                    return
            page += 1

    def process(self, task, browser, models, config):
        key = task["erp_goods_id"]
        self.store.log("处理任务：" + task["title"], key)
        if not task.get("supplier_sku_id"):
            evidence, match = [], None
            try:
                if not task.get("erp_sku"):
                    raise ValueError("ERP未提供目标SKU规格，请补全后重试")
                for candidate in browser.candidates(task):
                    result, reviews = models.match(task, candidate)
                    evidence.append({"candidate": candidate, "reviews": reviews})
                    self.store.update(key, match_evidence=evidence)
                    if result:
                        match = result
                        break
                if not match or not match.get("merchant_id"):
                    raise ValueError("候选均未通过图片、规格和SKU双重审核")
                sku = match["selected_sku"]
                task = self.store.update(key, stage="matched", supplier_url=match["url"],
                                         merchant_id=match["merchant_id"], supplier_sku_id=sku["id"],
                                         supplier_sku=sku["label"], cost_price=sku["price"],
                                         match_confidence=match["confidence"], match_evidence=evidence)
            except (CircuitOpen, Stopped):
                raise
            except Exception as exc:
                self.store.exception(key, "1688同款货源未匹配到", exc)
                return
        if task.get("weight_g"):
            self.finish(task, browser, config)
            return
        quota = self.store.quota()
        now = time.time()
        if quota["today"] >= config["daily_limit"]:
            tomorrow = datetime.fromtimestamp(now, CHINA).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            self.defer(key, tomorrow.timestamp(), "今日咨询已达上限，剩余任务次日继续")
            return
        if self.store.counts()["waiting_merchant_reply"] >= config["max_waiting"]:
            self.defer(key, now + 15, "已有两个或配置上限数量的等待会话")
            return
        if quota["last"] is not None and now - quota["last"] < config["consult_interval_seconds"]:
            self.defer(key, quota["last"] + config["consult_interval_seconds"], "等待商家咨询最小间隔")
            return
        page = None
        try:
            page, url, baseline = browser.prepare_chat(task)
            message = (random.choice(config["phrases"]) + "\n商品：" + task["supplier_url"]
                       + "\n规格：" + task["supplier_sku"] + "（询问单件含包装总重量）")
            result = self.store.reserve(task, config, message, url, baseline)
            if result == "duplicate":
                self.store.exception(key, "该商家已咨询过，永久去重禁止重复发送")
                return
            if result != "ok":
                self.defer(key, time.time() + 60, "咨询限流：" + result)
                return
            browser.send(page, task, message)
            self.store.sent(key)
            self.store.log("已发出重量咨询，进入等待商家回复", key)
        except CircuitOpen:
            if self.store.get(key)["stage"] == "send_reserved":
                self.store.exception(key, "发送时触发风控，结果不确定，请人工核对")
            raise
        except Stopped:
            if self.store.get(key)["stage"] == "send_reserved":
                self.store.exception(key, "咨询发送中断，结果不确定，请人工核对")
            raise
        except Exception as exc:
            self.store.exception(key, "商家会话打开或发送失败，禁止自动重复发送", exc)
        finally:
            if page:
                browser.release(page)

    def defer(self, key, until, reason):
        task = self.store.get(key)
        self.store.update(key, next_attempt_at=until, defer_reason=reason)
        if task.get("defer_reason") != reason:
            self.store.log(reason, key)

    def poll(self, task, browser, models, config, now):
        key = task["erp_goods_id"]
        try:
            replies = browser.replies(task)
            if not replies:
                if now >= task["deadline"]:
                    self.store.exception(key, "商家超时未回复重量咨询")
                else:
                    self.store.update(key, next_poll_at=now + config["poll_minutes"] * 60)
                return
            text = "\n".join(r["text"] for r in replies)
            self.store.update(key, merchant_reply=text, raw_replies=replies)
            weight = models.weight(text)
            if weight is None:
                self.store.exception(key, "AI无法识别包装重量")
                return
            task = self.store.update(key, status="pending", stage="weight_extracted", weight_g=weight)
            self.finish(task, browser, config)
        except (CircuitOpen, Stopped):
            raise
        except Exception as exc:
            # DOM/model errors retain the raw conversation for review, never imply a valid weight.
            self.store.exception(key, "AI无法识别包装重量", exc)

    def finish(self, task, browser, config):
        key = task["erp_goods_id"]
        try:
            if task["status"] == "exception":
                raise ValueError("异常任务禁止回写")
            confidence = number(task.get("match_confidence"))
            if not number(config["match_threshold"]) < confidence <= 1 or not task.get("supplier_sku_id") or not task.get("match_evidence"):
                raise ValueError("同款或SKU审核证据不完整")
            number(task.get("cost_price"))
            validation = validate_weight(task, config)
            task = self.store.update(key, validation=validation, stage="validated")
        except ValueError as exc:
            self.store.exception(key, "重量校验误差超出阈值", exc)
            return
        if not config["writeback_enabled"]:
            self.store.exception(key, "校验通过，等待启用ERP回写后手动重试")
            return
        try:
            if not task.get("erp_edit_url"):
                raise ValueError("缺少从ERP页面采集的商品编辑地址")
            def before_save(old):
                self.store.update(key, stage="writing", erp_before=old,
                                  write_intent={"cost_price": task["cost_price"], "weight_g": task["weight_g"], "at": time.time()})
            browser.write(task, before_save)
            self.store.update(key, status="success", stage="done", exception_reason="", exception_detail="", saved_at=time.time())
            self.store.log("ERP保存及刷新回读验证通过", key)
        except CircuitOpen:
            self.store.exception(key, "ERP回写保存失败", "页面风控触发；请核对是否已保存")
            raise
        except Stopped:
            self.store.exception(key, "ERP回写保存失败", "保存过程被停止，请核对原表单")
            raise
        except Exception as exc:
            self.store.exception(key, "ERP回写保存失败", exc)

    def edit(self, key, values, actor):
        allowed = {"cost_price", "weight_g", "reference_weight_g", "measured_weight_g", "erp_sku", "erp_edit_url", "note"}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("只能编辑成本、重量、参考重量、SKU、编辑地址和备注")
        with self.idle():
            task = self.store.get(key)
            if task["status"] != "exception":
                raise ValueError("仅异常任务允许人工编辑")
            for field in allowed & values.keys():
                if field.endswith("_g") or field == "cost_price":
                    values[field] = str(number(values[field])) if values[field] not in ("", None) else None
                elif not isinstance(values[field], str) or len(values[field]) > 2000:
                    raise ValueError("文本字段无效")
            if values.get("erp_edit_url"):
                from .config import safe_url
                safe_url(values["erp_edit_url"], host=urlsplit(self.config.load()["erp_list_url"]).hostname)
            history = task.get("manual_history", [])
            history.append({"actor": actor, "at": time.time(), "before": {k: task.get(k) for k in values}, "after": dict(values)})
            if "erp_sku" in values and values["erp_sku"] != task.get("erp_sku"):
                values.update(supplier_sku_id=None, match_confidence=None, match_evidence=[], weight_g=None,
                              conversation_id=None, conversation_url=None)
            self.store.update(key, **values, validation=None, manual_history=history)
            self.store.log("人工修改异常任务（原值保留审计记录）：" + actor, key)
            self.store.export()

    def retry(self, key):
        with self.idle():
            task = self.store.get(key)
            if task["status"] != "exception":
                raise ValueError("只能重试异常任务")
            changes = {"status": "pending", "exception_reason": "", "exception_detail": "", "next_attempt_at": 0,
                       "stage": "matched" if task.get("supplier_sku_id") else "collected"}
            if task.get("conversation_url") and not task.get("weight_g"):
                changes.update(status="waiting_merchant_reply", stage="waiting", next_poll_at=0,
                               deadline=time.time() + self.config.load()["timeout_minutes"] * 60)
            history = task.get("retry_history", [])
            history.append({"at": time.time(), "reason": task.get("exception_reason"), "stage": task["stage"]})
            self.store.update(key, **changes, retry_history=history)
            self.store.log("任务已重新排队；保留商家永久去重，不重复发送咨询", key)
            self.store.export()
