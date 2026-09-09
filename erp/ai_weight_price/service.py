import hashlib
import random
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from bit.bit_runtime_lock import InterProcessLock
from .browser import Browser, CircuitOpen, Stopped, NoExactMatch
from .config import Config, selection_key, selection_params
from .credentials import api_key
from .edge import debugger_identity, open_edge
from .models import Models, number, validate_weight
from .pricing import exchange_rate, usd_cost
from .store import CHINA, Store, COMPLETED
from .supplier_adapter import SupplierAdaptationError


class ItemBlocked(ValueError):
    """The current item needs attention before the next can be collected."""


class RunLimitReached(Exception):
    pass


class Service:
    def __init__(self, root, browser_factory=Browser, models_factory=Models):
        self.store = Store(root)
        self.config = Config(root)
        self.browser_factory, self.models_factory = browser_factory, models_factory
        self.stop_event = threading.Event()
        self.thread = None
        self.guard = threading.RLock()
        self.lock_key = "ai_weight_price_" + hashlib.sha256(str(self.store.root.resolve()).encode()).hexdigest()[:16]
        self._migrate_browser_attention_pause()

    def _migrate_browser_attention_pause(self):
        """Expose old login-redirect exceptions through the resumable pause UI."""
        if self.store.state("circuit"):
            return
        current = self.store.state("pipeline_current", {}) or {}
        key = current.get("task_id")
        if not key:
            return
        try:
            task = self.store.get(key)
        except KeyError:
            return
        detail = "：".join(str(task.get(k) or "") for k in ("exception_reason", "exception_detail"))
        if task["status"] == "exception" and re.search(r"login\.taobao|人机|验证码|需要登录|登录页|登录状态", detail, re.I):
            reason = "1688需要登录或人机审核；请在可见Edge中处理后返回控制台继续执行"
            self.store.set_state("circuit", {"kind": "browser_attention", "reason": reason, "at": time.time()})
            self.store.log("已将旧版1688登录跳转异常迁移为可恢复暂停；当前商品和原始异常记录已保留", key, "WARNING")

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
                "run_error": self.store.state("run_error"), "action_error": self.store.state("action_error"),
                "storage": str(self.store.root), "collection": self.store.state("collection"),
                "login": self.store.state("login", {"confirmed": False}),
                "selection": self.store.state("run_selection"),
                "visual_progress": self.store.state("visual_progress", {}),
                "model_connection": {**self.store.state("model_connection", {}),
                                     "configured": bool(api_key(self.config.load()["api_key_env"]))}}

    def check_model_connection(self):
        with self.idle():
            config = self.config.load()
            if not api_key(config["api_key_env"]):
                raise ValueError("请在本机环境变量 " + config["api_key_env"] + " 中设置模型密钥")
            result = {"configured": True, "checked_at": time.time(), "ok": False, "model": config["model"]}
            try:
                answer = self.models_factory(config, self.store.log).call(config["model"], "Reply with exactly OK.")
                if answer.strip() != "OK":
                    raise ValueError("模型未返回预期的连接检查结果")
                result["ok"] = True
                self.store.log("模型连接检查通过：" + config["model"] + "；服务已读取本机模型密钥（不记录密钥内容）")
            except Exception as exc:
                # HTTP/client exceptions may carry request objects; only retain
                # their type here, never credentials or a provider response.
                result["error_type"] = type(exc).__name__
                self.store.log("模型连接检查失败：" + type(exc).__name__, level="ERROR")
                raise ValueError("模型连接检查失败：" + type(exc).__name__) from None
            finally:
                self.store.set_state("model_connection", result)
            return result

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

    def open_supplier_login(self):
        with self.idle():
            config = self.config.load()
            with self.browser_factory(config, threading.Event(), self.store.log) as browser:
                browser.open_supplier_login()
            self.store.log("已在核重核价使用的Edge窗口打开1688，请完成1688登录后继续；智赢登录状态保留")

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
        required = ["erp_rows", "erp_title", "erp_image", "erp_next", "erp_category_control", "erp_page_active", "erp_page_first"] if mode in ("collect", "pipeline") else []
        if mode in ("process", "pipeline"):
            if not task.get("supplier_sku_id"):
                required += ["image_search_upload", "result_links"]
                if not config["supplier_auto_adapt"]:
                    required += ["supplier_title", "supplier_image", "supplier_merchant",
                                 "supplier_merchant_attribute", "sku_rows", "sku_id_attribute", "sku_label"]
            # Chat is a fallback. Missing chat controls must not block products
            # whose page provides both weight and final variant cost.
        if mode in ("process", "pipeline") and config["writeback_enabled"] and config["workflow_mode"] == "legacy_consult":
            required += ["erp_edit_id", "erp_edit_sku", "erp_net_income_input", "erp_weight_input", "erp_save", "erp_saved"]
        missing = [key for key in required if not config["selectors"][key]]
        if missing:
            raise ValueError("首次运行需要完成页面适配，缺少DOM字段：" + "、".join(missing))
        if mode in ("process", "pipeline") and (config["workflow_mode"] == "image_first" or not task.get("supplier_sku_id") or self.missing_info(task)) and urlsplit(config["api_base_url"]).hostname not in ("localhost", "127.0.0.1", "::1") and not api_key(config["api_key_env"]):
            raise ValueError("请在本机环境变量 " + config["api_key_env"] + " 中设置模型密钥")

    def start(self, mode="pipeline", task_id=None, selection=None, max_items=10):
        if not isinstance(mode, str) or mode not in ("collect", "process", "pipeline", "probe"):
            raise ValueError("运行模式无效")
        if type(max_items) is not int or not 1 <= max_items <= 10000:
            raise ValueError("本次商品数量必须是1–10000的整数")
        label = {"collect": "采集", "process": "核重核价处理", "pipeline": "逐件采集核重核价", "probe": "Edge连接检查"}[mode]
        self.store.log(f"收到{label}启动请求，正在检查运行条件", task_id)
        with self.guard:
            config = self.config.load()
            if mode != "probe":
                self.require_login(config)
                if self.store.state("circuit"):
                    raise ValueError("当前正在等待处理1688登录或人机审核，请完成后使用继续执行开关")
                if not task_id:
                    selection = selection_params(selection if selection is not None else self.store.state("run_selection"), config)
                    if selection["category"] and not any(c["value"] == selection["category"] for c in self.store.state("categories", [])):
                        raise ValueError("请选择已读取的智赢分类，或留空表示不筛选分类")
                    config["run_selection"] = selection
                    config["run_scope"] = selection_key(selection, config)
                    if mode == "process" and not self.store.list(scope=config["run_scope"])["total"]:
                        raise ValueError("所选分类及页码范围尚无采集任务，请先点击“采集所选范围”")
                config["max_items"] = 1 if task_id else max_items
                config["run_id"] = uuid.uuid4().hex
                batch = {"run_id": config["run_id"], "mode": mode, "selection": selection,
                         "max_items": config["max_items"], "started_at": time.time(), "outcome": "preflight"}
                try:
                    self.preflight(config, mode, task_id)
                except ValueError as exc:
                    batch.update(outcome="blocked", message=str(exc), finished_at=time.time())
                    self.store.save_run(batch)
                    selected = [self.store.get(task_id)] if task_id else self.store.list("pending", page_size=max_items, scope=config.get("run_scope"))["rows"]
                    for item in selected:
                        key = item["erp_goods_id"]
                        self.store.record_run_item(config["run_id"], key, execution_result="启动受阻", execution_reason=str(exc))
                        self.store.log(f"批次 {config['run_id']}：商品 {key} 启动检查未通过，未执行回填：{exc}", key, "ERROR")
                    raise
            lock = self.lock()
            if not lock.acquire():
                raise ValueError("已有进程正在运行此模块")
            try:
                self.stop_event.clear()
                self.store.set_state("stop_requested", False)
                self.store.set_state("run_error", None)
                self.store.set_state("action_error", None)
                if selection:
                    self.store.set_state("run_selection", selection)
                self.store.set_state("run", {"mode": mode, "selection": selection, "started_at": time.time(),
                                             "task_id": task_id,
                                             "run_id": config.get("run_id"), "max_items": config.get("max_items"), "processed_items": 0,
                                             "success_items": 0, "skipped_items": 0, "blocked_items": 0, "risk_items": 0,
                                             "outcome": "running", "message": "正在连接本机Edge"})
                if config.get("run_id"):
                    self.store.save_run(self.store.state("run"))
                self.store.log(f"{label}已启动，正在连接本机Edge", task_id)
                self.thread = threading.Thread(target=self.run, args=(config, mode, task_id, lock), daemon=True)
                self.thread.start()
            except Exception as exc:
                lock.release()
                self.store.set_state("run_error", str(exc))
                self.store.set_state("run", {"mode": mode, "outcome": "failed", "finished_at": time.time(), "message": "启动失败：" + str(exc)})
                self.store.log("启动失败：" + str(exc), task_id, "ERROR")
                raise

    def stop(self):
        self.stop_event.set()
        self.store.set_state("stop_requested", True)
        self.store.log("已请求停止；保留进度及商家去重记录")

    def circuit(self, error):
        self.store.set_state("circuit", {"kind": "browser_attention", "reason": str(error), "at": time.time()})
        self.store.log("等待人工处理1688登录或人机审核，当前商品和批次进度已保留：" + str(error), level="WARNING")

    def _requeue(self, key):
        task = self.store.get(key)
        changes = {"status": "pending", "exception_reason": "", "exception_detail": "", "skip_reason": "", "next_attempt_at": 0,
                   "stage": "matched" if task.get("supplier_sku_id") else "collected"}
        if task.get("conversation_url") and self.missing_info(task):
            changes.update(status="waiting_merchant_reply", stage="waiting", next_poll_at=0,
                           deadline=time.time() + self.config.load()["timeout_minutes"] * 60)
        history = task.get("retry_history", [])
        history.append({"at": time.time(), "reason": task.get("decision_reason") or task.get("skip_reason") or task.get("exception_reason"),
                        "stage": task["stage"]})
        self.store.update(key, **changes, retry_history=history)

    def continue_after_human(self):
        """Clear a browser-attention pause and resume from the retained item."""
        with self.idle():
            pause = self.store.state("circuit")
            if not pause:
                raise ValueError("当前没有等待处理的1688登录或人机审核")
            previous = self.store.state("run", {}) or {}
            mode = previous.get("mode") if previous.get("mode") in ("pipeline", "process") else "pipeline"
            task_id = previous.get("task_id") if mode == "process" else None
            selection = previous.get("selection") or self.store.state("run_selection")
            maximum = int(previous.get("max_items") or 1)
            remaining = max(1, maximum - int(previous.get("processed_items") or 0))
            current = self.store.state("pipeline_current", {}) or {}
            current_id = current.get("task_id")
            if current_id:
                task = self.store.get(current_id)
                old_error = "：".join(str(task.get(k) or "") for k in ("exception_reason", "exception_detail"))
                # Compatibility for runs made before browser redirects were
                # represented as a resumable pause.
                if task["status"] == "exception" and re.search(r"login\.taobao|人机|验证码|需要登录|登录页|登录状态", old_error, re.I):
                    self._requeue(current_id)
                    self.store.log("旧版登录跳转异常已恢复为待处理，继续时重试当前商品", current_id, "WARNING")
            self.store.set_state("circuit", None)
            self.store.set_state("stop_requested", False)
            self.store.log("人工确认已完成1688登录或人机审核；从当前商品继续执行", current_id, "WARNING")
        try:
            self.start(mode, task_id, selection, remaining)
        except Exception:
            self.store.set_state("circuit", pause)
            raise
        return self.status()

    def run(self, config, mode, task_id, lock):
        outcome, message = "completed", "运行完成"
        try:
            self.store.recover()
            with self.browser_factory(config, self.stop_event, self.store.log) as browser:
                if mode == "probe":
                    message = "Edge连接检查通过；未采集、发消息或回写"
                    return
                self.store.log("正在检查智赢登录状态")
                browser.confirm_login()
                self.store.log("智赢登录状态检查通过")
                if mode == "collect":
                    self.store.set_state("run", {**self.store.state("run", {}), "message": "正在采集所选范围"})
                    browser.collect(self.store)
                    count = self.store.list(scope=config.get("run_scope"))["total"]
                    message = f"采集完成，所选范围共 {count} 条商品；可开始核重核价处理"
                    return
                self.store.set_state("run", {**self.store.state("run", {}), "message": "正在处理所选任务"})
                models = self.models_factory(config, self.store.log)
                browser.adapt_supplier = getattr(models, "supplier_dom", None)
                browser.record_supplier_adaptation = self.record_supplier_adaptation
                browser.record_visual = self.record_visual
                if mode == "pipeline":
                    if config["workflow_mode"] == "image_first":
                        current = self.store.state("pipeline_current", {}) or {}
                        if current.get("scope") == config.get("run_scope") and current.get("task_id"):
                            self.complete_one(current["task_id"], browser, models, config)
                        browser.collect(self.store, on_task=lambda key: self.complete_one(key, browser, models, config))
                        message = self.completion_message("所选范围处理完成")
                        return
                    current = self.store.state("pipeline_current", {}) or {}
                    if current.get("scope") == config.get("run_scope") and current.get("task_id"):
                        self.complete_one(current["task_id"], browser, models, config)
                    # Finish previously collected items before advancing an old
                    # page checkpoint, including upgrades from batch collection.
                    while True:
                        existing = self.next_task(config)
                        if not existing:
                            break
                        self.complete_one(existing["erp_goods_id"], browser, models, config)
                    browser.collect(self.store, on_task=lambda key: self.complete_one(key, browser, models, config))
                    message = self.completion_message("所选范围处理完成")
                    return
                while not self.stop_event.is_set() and not self.store.state("stop_requested", False):
                    if self.store.state("circuit"):
                        self.store.log("存在1688人工处理暂停，需完成登录或人机审核后继续", level="WARNING")
                        break
                    selected = self.store.get(task_id) if task_id else self.next_task(config)
                    if not selected:
                        break
                    self.complete_one(selected["erp_goods_id"], browser, models, config)
                    self.store.export()
                    if task_id:
                        if self.store.get(task_id)["status"] in (*COMPLETED, "exception"):
                            break
                    elif not any(self.store.counts(config.get("run_scope"))[s] for s in ("pending", "waiting_merchant_reply")):
                        break
                    if self.stop_event.wait(1):
                        break
                if self.store.state("circuit"):
                    outcome, message = "blocked", "运行等待人工处理1688登录或人机审核；处理完成后点击继续执行"
                elif self.stop_event.is_set() or self.store.state("stop_requested", False):
                    outcome, message = "stopped", "运行已停止，进度已保存"
                else:
                    message = "本次处理已结束，请查看商品状态与异常原因"
        except RunLimitReached:
            message = self.completion_message("达到本次数量上限，已停止；可导出本次Excel")
        except CircuitOpen as exc:
            self.circuit(exc)
            outcome, message = "blocked", "运行等待人工处理，当前商品未跳过：" + str(exc)
        except ItemBlocked as exc:
            outcome, message = "blocked", str(exc)
        except Stopped:
            outcome, message = "stopped", "运行已停止，进度已保存"
        except Exception as exc:
            outcome, message = "failed", f"运行失败：{type(exc).__name__}: {exc}"
            self.store.set_state("run_error", str(exc))
        finally:
            try:
                self.store.set_state("run", {**self.store.state("run", {}), "mode": mode,
                                            "finished_at": time.time(), "outcome": outcome, "message": message})
                self.store.log(message, task_id, "ERROR" if outcome == "failed" else "WARNING" if outcome == "blocked" else "INFO")
                if config.get("run_id"):
                    self.store.save_run(self.store.state("run"))
                self.store.export()
            finally:
                lock.release()

    def record_supplier_adaptation(self, key, event):
        history = self.store.get(key).get("supplier_adaptations", [])
        self.store.update(key, supplier_adaptations=[*history, event][-20:])
        self.store.set_state("supplier_adaptation", {"task_id": key, **event})

    def record_visual(self, key, step, message, page_url="", picture=None):
        if page_url and (urlsplit(page_url).hostname or "").startswith(("login.", "passport.")):
            parsed = urlsplit(page_url)
            page_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        task = self.store.get(key)
        event = {"id": uuid.uuid4().hex, "at": time.time(), "step": step,
                 "message": message, "page_url": page_url}
        if picture:
            folder = self.store.root / "visuals"
            folder.mkdir(parents=True, exist_ok=True)
            filename = event["id"] + ".jpg"
            (folder / filename).write_bytes(picture)
            event["screenshot_url"] = "/api/ai-weight-price/visuals/" + filename
        run_id = self.store.state("run", {}).get("run_id")
        event["run_id"] = run_id
        history = [*(task.get("visual_history") or []), event]
        self.store.update(key, visual_history=history)
        self.store.set_state("visual_progress", {"task_id": key, "title": task["title"],
                             "main_image_url": task.get("main_image_url", ""),
                             "steps": [e for e in history if e.get("run_id") == run_id][-40:]})
        self.progress(key, message)

    def completion_message(self, prefix):
        run = self.store.state("run", {})
        return f"{prefix}。本次完成 {run.get('processed_items', 0)} 件：成功 {run.get('success_items', 0)} 件，屏蔽 {run.get('blocked_items', 0)} 件，风险 {run.get('risk_items', 0)} 件，跳过 {run.get('skipped_items', 0)} 件"

    def next_task(self, config):
        for status in ("waiting_merchant_reply", "pending"):
            tasks = self.store.list(status, page_size=1, scope=config.get("run_scope"))["rows"]
            if tasks:
                return tasks[0]
        return None

    def progress(self, key, message):
        self.store.set_state("run", {**self.store.state("run", {}), "current_task_id": key, "message": message})
        self.store.log(message, key)

    def complete_one(self, key, browser, models, config):
        if self.store.get(key)["status"] in COMPLETED:
            current = self.store.state("pipeline_current", {}) or {}
            if current.get("task_id") == key:
                self.store.set_state("pipeline_current", None)
            return
        run_id = config.get("run_id")
        self.store.record_run_item(run_id, key, execution_result="执行中")
        try:
            self._complete_one(key, browser, models, config)
        finally:
            current = self.store.get(key)
            result = {"success": "处理成功", "blocked": "屏蔽", "risk": "风险", "exception": "异常", "skipped": "已跳过（未完全匹配）", "waiting_merchant_reply": "等待商家回复", "pending": "待处理"}[current["status"]]
            reason = ("：".join(str(current.get(field) or "") for field in ("exception_reason", "exception_detail") if current.get(field))
                      if current["status"] == "exception" else current.get("decision_reason") or current.get("skip_reason", ""))
            self.store.record_run_item(run_id, key, execution_result=result, execution_reason=reason)
        run = self.store.state("run", {})
        processed = run.get("processed_items", 0) + 1
        counter = current["status"] + "_items"
        self.store.set_state("run", {**run, "processed_items": processed, counter: run.get(counter, 0) + 1})
        if config.get("max_items") and processed >= config["max_items"]:
            raise RunLimitReached()

    def _complete_one(self, key, browser, models, config):
        current = self.store.state("pipeline_current", {}) or {}
        scope = config.get("run_scope")
        if scope is None and current.get("task_id") == key:
            scope = current.get("scope")
        self.store.set_state("pipeline_current", {"scope": scope, "task_id": key})
        self.progress(key, f"当前商品 {key}：逐件主图搜货；完全匹配后核重核价，未完全匹配则记录并跳过")
        while True:
            if self.stop_event.is_set() or self.store.state("stop_requested", False):
                raise Stopped()
            if self.store.state("circuit"):
                raise ItemBlocked(f"商品 {key} 等待完成1688登录或人机审核；保留当前进度")
            task = self.store.get(key)
            if task["status"] == "success":
                self.progress(key, f"商品 {key} 处理成功：包装重量 {task.get('weight_g')}g，净收益 ${task.get('net_income_usd')}；继续下一件")
                self.store.set_state("pipeline_current", None)
                return
            if task["status"] in ("skipped", "blocked", "risk"):
                self.store.set_state("pipeline_current", None)
                return
            if task["status"] == "exception":
                reason = "：".join(str(task.get(field) or "") for field in ("exception_reason", "exception_detail") if task.get(field))
                raise ItemBlocked(f"商品 {key} 未完成，已暂停后续商品：{reason}。请处理异常后继续")
            self.tick(browser, models, config, key)
            self.store.export()
            task = self.store.get(key)
            self.store.record_run_item(config.get("run_id"), key)
            if task["status"] in (*COMPLETED, "exception"):
                continue
            message = (f"商品 {key} 等待千牛回复，收到完整重量和成本后回填，再继续下一件"
                       if task["status"] == "waiting_merchant_reply" else f"商品 {key} 暂缓：{task.get('defer_reason', '')}")
            if self.store.state("run", {}).get("message") != message:
                self.progress(key, message)
            self.stop_event.wait(1)

    def tick(self, browser, models, config, task_id=None):
        now = time.time()
        task = self.store.get(task_id) if task_id else self.next_task(config)
        if not task or self.stop_event.is_set() or self.store.state("circuit"):
            return
        if task["status"] == "waiting_merchant_reply":
            if now >= min(task["next_poll_at"], task["deadline"]):
                self.poll(task, browser, models, config, now)
            return
        if task["status"] == "pending" and task.get("next_attempt_at", 0) <= now:
            self.process(task, browser, models, config)

    @staticmethod
    def missing_info(task):
        return [field for field in ("weight_g", "cost_price") if not task.get(field)]

    @staticmethod
    def page_text(match):
        sku = match["selected_sku"]
        return "\n".join(str(value) for value in (
            "目标规格：" + sku["label"], sku.get("raw_text", ""), sku.get("raw_weight", ""),
            "页面单价：" + str(sku.get("raw_price", "")), "变体加价：" + str(sku.get("raw_surcharge", "")),
            match.get("raw_weight", ""), match.get("description", "")) if value)

    def process(self, task, browser, models, config):
        if config["workflow_mode"] == "image_first":
            from .image_first import process
            return process(self, task, browser, models, config)
        key = task["erp_goods_id"]
        self.store.log("处理任务：" + task["title"], key)
        if not task.get("supplier_sku_id"):
            evidence, match = [], None
            try:
                self.progress(key, f"商品 {key}：使用主图以图搜货，核对同款和目标变体")
                for candidate in browser.candidates(task):
                    if not task.get("erp_sku"):
                        raise NoExactMatch("ERP未提供目标SKU规格，无法确认完全匹配")
                    result, reviews = models.match(task, candidate)
                    evidence.append({"candidate": candidate, "reviews": reviews})
                    self.store.update(key, match_evidence=evidence)
                    if result:
                        match = result
                        break
                    reasons = "；".join(str(review.get("reason") or "未确认同款及完整规格")[:400] for review in reviews if isinstance(review, dict))
                    self.record_visual(key, "candidate_rejected", f"候选 {len(evidence)} 未完全匹配：{reasons or '审核未通过'}", page_url=candidate.get("url", ""))
                if not match or not match.get("merchant_id"):
                    reason = f"已核验 {len(evidence)} 个候选，均未通过图片、规格和SKU双重审核" if evidence else "以图搜货未找到可核验的完全匹配货源"
                    raise NoExactMatch(reason)
                sku = match["selected_sku"]
                task = self.store.update(key, stage="matched", supplier_url=match["url"],
                                         merchant_id=match["merchant_id"], supplier_sku_id=sku["id"],
                                         supplier_sku=sku["label"], cost_price=sku.get("price"),
                                         supplier_page_text=self.page_text(match), page_info_checked=False,
                                         info_sources={"cost_price": "1688目标变体价格"} if sku.get("price") else {},
                                         supplier_price_evidence=sku, net_income_usd=None, pricing=None,
                                         match_confidence=match["confidence"], match_evidence=evidence)
            except NoExactMatch as exc:
                self.store.skip(key, str(exc))
                self.record_visual(key, "skipped", "未完全匹配，已记录并跳过，继续下一件：" + str(exc))
                return
            except SupplierAdaptationError as exc:
                self.store.exception(key, str(exc))
                return
            except (CircuitOpen, Stopped):
                raise
            except Exception as exc:
                self.store.exception(key, "1688搜货执行异常", exc)
                return
        if self.missing_info(task) and not task.get("page_info_checked"):
            try:
                self.progress(key, f"商品 {key}：先读取1688页面的包装重量和变体成本")
                text = task.get("supplier_page_text")
                if text is None:
                    text = browser.supplier_page(task)
                info = models.supplier_info(task, text) if text.strip() else {}
                changes = {field: info[field] for field in self.missing_info(task) if info.get(field)}
                task = self.store.update(key, **changes, page_info_checked=True, supplier_page_text=text,
                                         page_info=info, stage="page_checked",
                                         info_sources={**task.get("info_sources", {}), **{field: "1688页面" for field in changes}})
            except (CircuitOpen, Stopped):
                raise
            except Exception as exc:
                self.store.exception(key, "1688页面资料读取失败", exc)
                return
        if not self.missing_info(task):
            self.progress(key, f"商品 {key}：重量和成本齐全，计算美元净收益并回填")
            self.finish(task, browser, config)
            return
        missing = self.missing_info(task)
        labels = {"weight_g": "单件含包装总重量（克）", "cost_price": "包含变体加价的最终人民币单件供货成本"}
        if not task.get("consult_missing"):
            self.progress(key, f"商品 {key}：1688页面缺少{'、'.join(labels[field] for field in missing)}，转千牛咨询")
            task = self.store.update(key, consult_missing=missing)
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
            greeting = random.choice(config["phrases"]) if "weight_g" in missing else "您好，想咨询下这款商品的供货成本。"
            message = (greeting + "\n商品：" + task["supplier_url"] + "\n规格：" + task["supplier_sku"]
                       + "\n请确认：" + "；".join(labels[field] for field in missing) + "。请按上述规格一件报价，包含变体加价。")
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
            if task.get("cost_price"):
                info = {"weight_g": models.weight("目标规格：" + task["supplier_sku"] + "\n" + text)} if not task.get("weight_g") else {}
            else:
                info = models.supplier_info(task, text, source="千牛商家回复")
            changes = {field: info[field] for field in self.missing_info(task) if info.get(field)}
            task = self.store.update(key, **changes, reply_info={**task.get("reply_info", {}), **info},
                                     info_sources={**task.get("info_sources", {}), **{field: "千牛回复" for field in changes}})
            if self.missing_info(task):
                if now >= task["deadline"]:
                    self.store.exception(key, "AI无法识别包装重量" if not task.get("weight_g") else "商家回复未提供明确的变体供货成本")
                else:
                    self.store.update(key, next_poll_at=now + config["poll_minutes"] * 60)
                return
            task = self.store.update(key, status="pending", stage="info_extracted")
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
        try:
            pricing = usd_cost(task["cost_price"], self.exchange_rate(config))
            task = self.store.update(key, net_income_usd=pricing["net_income_usd"], pricing=pricing)
            self.store.log(f"所选变体供货成本 ¥{task['cost_price']} ÷ 汇率 {pricing['cny_per_usd']}"
                           f" = ${pricing['unrounded_usd']}，向上取整 ${pricing['net_income_usd']}，将回填智赢净收益"
                           f"（汇率日期 {pricing['rate_date']}，来源 {pricing['rate_source']}）", key)
        except Exception as exc:
            self.store.update(key, net_income_usd=None, pricing=None)
            self.store.exception(key, "美元成本换算失败", exc)
            return
        if not config["writeback_enabled"]:
            self.store.exception(key, "校验通过，等待启用ERP回写后手动重试")
            return
        try:
            if not task.get("erp_edit_url"):
                raise ValueError("缺少从ERP页面采集的商品编辑地址")
            def before_save(old):
                intent = {"net_income_usd": task["net_income_usd"], "pricing": pricing,
                          "weight_g": task["weight_g"], "at": time.time()}
                history = self.store.get(key).get("write_history", [])
                history.append({"before": old, "intent": intent, "after": None, "verified": False, "at": intent["at"]})
                self.store.update(key, stage="writing", erp_before=old,
                                  erp_after=None, write_verified=False,
                                  write_intent=intent, write_history=history)
                self.store.log(f"回填前：重量 {old['weight_g']}g，净收益 ${old['net_income_usd']}；"
                               f"计划修改为：重量 {task['weight_g']}g，净收益 ${task['net_income_usd']}", key)
            actual = browser.write(task, before_save)
            if not actual or number(actual.get("net_income_usd")) != number(task["net_income_usd"]) or number(actual.get("weight_g")) != number(task["weight_g"]):
                raise ValueError("ERP未返回一致的保存后回读数据")
            saved_at = time.time()
            history = self.store.get(key).get("write_history", [])
            history[-1].update(after=actual, saved_at=saved_at, verified=True)
            self.store.update(key, status="success", stage="done", exception_reason="", exception_detail="", saved_at=saved_at,
                              erp_after=actual, write_verified=True, write_history=history)
            self.store.log(f"保存成功并回读确认：重量 {actual['weight_g']}g，美元净收益 ${actual['net_income_usd']}", key)
        except CircuitOpen:
            self.store.exception(key, "ERP回写保存失败", "页面风控触发；请核对是否已保存")
            raise
        except Stopped:
            self.store.exception(key, "ERP回写保存失败", "保存过程被停止，请核对原表单")
            raise
        except Exception as exc:
            actual = getattr(exc, "actual", None)
            history = self.store.get(key).get("write_history", [])
            if history and not history[-1].get("verified"):
                history[-1].update(after=actual, error=str(exc))
                self.store.update(key, erp_after=actual, write_verified=False, write_history=history)
                if actual:
                    self.store.log("保存后回读不一致：" + str(actual), key, "ERROR")
            self.store.exception(key, "ERP回写保存失败", exc)

    def exchange_rate(self, config):
        return exchange_rate(config, self.store)

    def edit(self, key, values, actor):
        allowed = {"cost_price", "weight_g", "reference_weight_g", "measured_weight_g", "erp_sku", "erp_edit_url", "note"}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("只能编辑成本、重量、参考重量、SKU、编辑地址和备注")
        with self.idle():
            task = self.store.get(key)
            if task["status"] not in ("exception", "skipped", "blocked", "risk"):
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
                              conversation_id=None, conversation_url=None, cost_price=None, supplier_page_text=None,
                              page_info_checked=False, page_info=None, info_sources={}, consult_missing=None)
            self.store.update(key, **values, validation=None, net_income_usd=None, pricing=None, manual_history=history)
            self.store.log("人工修改异常任务（原值保留审计记录）：" + actor, key)
            self.store.export()

    def retry(self, key):
        with self.idle():
            task = self.store.get(key)
            if task["status"] not in ("exception", "skipped", "blocked", "risk"):
                raise ValueError("只能重试异常、跳过、屏蔽或风险任务")
            self._requeue(key)
            self.store.log("任务已重新排队；保留商家永久去重，不重复发送咨询", key)
            self.store.export()
