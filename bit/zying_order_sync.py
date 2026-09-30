"""Read Zying orders through its authenticated UI, then update local purchases."""
import copy
import html
import re
import secrets
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

ORDER_URL = "https://meli.zying.net/#/order"
SEARCH_PLACEHOLDER = "订单、采购单、运单,多个编号可以逗号、空格分隔"
SYNC_BATCH_SIZE = 10
SYNC_PROGRESS_TIMEOUT = 600


def is_lookup_timeout(exc):
    if isinstance(exc, TimeoutError):
        return True
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:
        return False
    return isinstance(exc, PlaywrightTimeoutError)


def timeout_outcome(exc):
    return {"skip_reason": "智赢查询超时，已跳过并继续下一单：" + str(exc)[:200]}



def clean(value):
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def identifiers(value):
    return set(re.findall(r"[A-Za-z0-9_-]+", clean(value)))


def order_keys(row):
    return {clean(row.get(key)) for key in ("id", "order_number", "pack_id")} - {""}


def parse_purchase(detail, row):
    roots = detail.get("root") or []
    if len(roots) != 1:
        raise ValueError("智赢订单详情不唯一")
    root = roots[0]
    remote_keys = identifiers(root.get("order_no")) | identifiers(root.get("order_key"))
    if not remote_keys.intersection(order_keys(row)):
        raise ValueError("智赢详情订单号与泽顺订单不一致")
    # A pack match alone must not assign its entire cost to one child order.
    if clean(row.get("id")) not in remote_keys:
        raise ValueError("仅匹配到打包单，无法确认子订单成本归属")
    buys = detail.get("buys")
    if not isinstance(buys, list):
        raise ValueError("智赢采购数据格式异常")
    if not buys:
        return None
    changes = {}
    for target, source in (("purchase_order", "no"), ("purchase_tracking", "trace")):
        values = list(dict.fromkeys(clean(buy.get(source)) for buy in buys if clean(buy.get(source))))
        if values:
            value = ",".join(values)
            if len(value) > 255:
                raise ValueError("采购单号或物流号超过字段长度")
            changes[target] = value
    try:
        cost = Decimal(str(root.get("order_cost")))
    except InvalidOperation as exc:
        raise ValueError("智赢成本缺失或不可读取") from exc
    if not cost.is_finite() or cost < 0:
        raise ValueError("智赢成本异常")
    changes["purchase_cost"] = str(cost.quantize(Decimal("0.01")))
    return changes


def snapshot_orders(reader, filters, should_stop=None):
    params = dict(filters, page=1, page_size=200)
    result, seen = [], set()
    while True:
        if should_stop and should_stop():
            raise InterruptedError("任务已结束")
        data = reader(**params)
        rows = data.get("rows") or []
        for row in rows:
            key = clean(row.get("id"))
            if not key:
                raise ValueError("订单缺少内部编号")
            if key in seen:
                raise ValueError("订单列表在读取期间发生变化，请重新同步")
            seen.add(key)
            result.append(dict(row))
        if len(result) >= int(data.get("total") or 0):
            return result
        if not rows:
            raise ValueError("订单列表分页不完整，请重新同步")
        params["page"] += 1


def response_data(response):
    if not response.ok:
        raise RuntimeError("智赢请求失败，请检查登录状态")
    payload = response.json()
    if payload.get("code") != 200:
        raise RuntimeError("智赢请求未成功，请检查登录状态与订单查看权限")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("智赢响应异常或登录已失效，请重新登录")
    return data


class ZyingPage:
    def __init__(self, page):
        self.page = page
        self._ready = False

    def _ensure_order_page(self):
        page = self.page
        current = urlsplit(page.url)
        target = urlsplit(ORDER_URL)
        on_order_page = (
            (current.scheme, current.netloc, current.path)
            == (target.scheme, target.netloc, target.path)
            and current.fragment.split("?", 1)[0].rstrip("/") == target.fragment
        )
        field = page.get_by_placeholder(SEARCH_PLACEHOLDER, exact=True)
        if self._ready and on_order_page and field.is_visible():
            return
        self._ready = False
        # Login can redirect to another hash route on the same host. Navigate
        # back to orders after confirmation instead of waiting on that page.
        try:
            if not on_order_page:
                page.goto(ORDER_URL, wait_until="domcontentloaded", timeout=30000)
            field.wait_for(timeout=30000)
        except Exception as exc:
            current = urlsplit(page.url)
            route = current.fragment.split("?", 1)[0]
            if route.rstrip("/") == "/login" or current.path.rstrip("/") == "/login":
                message = "智赢仍停留在登录页，请在 Agent 打开的 Edge 中完成登录后重新启动同步"
            else:
                location = current.path + ("#" + route if route else "")
                message = (
                    f"已尝试切换智赢订单页，但未找到订单搜索框（当前页面：{location}）；"
                    "请确认该账号能打开订单列表，或检查智赢页面是否已改版"
                )
            raise RuntimeError(message) from exc
        self._ready = True

    @staticmethod
    def _query_key(rows):
        keys = []
        for row in rows:
            keys.extend(sorted(order_keys(row)))
        return ",".join(dict.fromkeys(keys))

    def _search(self, rows):
        page = self.page
        self._ensure_order_page()
        key = self._query_key(rows)
        if not key:
            raise ValueError("订单缺少可查询的智赢编号")
        # The previous order detail is a drawer/modal in the SPA. Escape closes
        # it so the same search field and result table can be reused.
        page.keyboard.press("Escape")
        field = page.get_by_placeholder(SEARCH_PLACEHOLDER, exact=True)
        field.fill(key)
        def searched(response):
            if "cmd=orders.load" not in response.url:
                return False
            body = response.request.post_data_json or {}
            return body.get("key") == key
        with page.expect_response(searched, timeout=30000) as pending:
            field.press("Enter")
        listing = response_data(pending.value).get("list")
        if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
            raise RuntimeError("智赢订单列表格式异常")
        try:
            too_many = int(listing.get("maxcount") or 0) > len(listing["data"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("智赢订单列表格式异常") from exc
        return listing["data"], too_many

    def _detail(self, internal_id):
        page = self.page
        page.keyboard.press("Escape")
        def detailed(response):
            return ("cmd=order.view" in response.url and
                    str((response.request.post_data_json or {}).get("id")) == internal_id)
        with page.expect_response(detailed, timeout=30000) as pending:
            page.locator(f'tr[data-row-key="{internal_id}"]').click()
        detail = response_data(pending.value)
        if clean((detail.get("root") or [{}])[0].get("order_id")) != internal_id:
            raise ValueError("智赢返回了其他订单详情")
        return detail

    def lookup_many(self, rows):
        """Search several orders in the SPA, splitting groups that overflow one page."""
        outcomes = {}

        def search_group(group):
            try:
                listing, too_many = self._search(group)
            except Exception as exc:
                if not is_lookup_timeout(exc):
                    raise
                for row in group:
                    outcomes[clean(row.get("id"))] = timeout_outcome(exc)
                return
            if too_many:
                if len(group) > 1:
                    middle = len(group) // 2
                    search_group(group[:middle])
                    search_group(group[middle:])
                    return
                outcomes[clean(group[0].get("id"))] = {"error": "智赢搜索结果超过一页，无法确认唯一订单"}
                return

            matches_by_id = {}
            for row in group:
                row_id = clean(row.get("id"))
                row_keys = order_keys(row)
                matches = [item for item in listing if row_keys.intersection(
                    identifiers(item.get("no")) | identifiers(item.get("key")))]
                exact = [item for item in matches if row_id in
                         identifiers(item.get("no")) | identifiers(item.get("key"))]
                matches = exact or matches
                if not matches:
                    outcomes[row_id] = {"detail": None}
                    continue
                if len(matches) != 1:
                    outcomes[row_id] = {"error": "智赢找到多个对应订单，请人工核对"}
                    continue
                internal_id = clean(matches[0].get("id"))
                if not internal_id.isdigit():
                    outcomes[row_id] = {"error": "智赢内部订单号异常"}
                    continue
                matches_by_id.setdefault(internal_id, []).append(row_id)

            for internal_id, row_ids in matches_by_id.items():
                try:
                    detail = self._detail(internal_id)
                except Exception as exc:
                    if is_lookup_timeout(exc):
                        outcome = timeout_outcome(exc)
                    elif isinstance(exc, ValueError):
                        outcome = {"error": str(exc)}
                    else:
                        raise
                    for row_id in row_ids:
                        outcomes[row_id] = outcome
                else:
                    for row_id in row_ids:
                        outcomes[row_id] = {"detail": detail}

        for start in range(0, len(rows), SYNC_BATCH_SIZE):
            search_group(rows[start:start + SYNC_BATCH_SIZE])
        return outcomes

    def lookup(self, row):
        outcome = self.lookup_many([row]).get(clean(row.get("id")), {})
        if outcome.get("skip_reason"):
            raise TimeoutError(outcome["skip_reason"])
        if outcome.get("error"):
            raise ValueError(outcome["error"])
        return outcome.get("detail")


def lookup_many(reader, rows):
    """Keep test/custom readers compatible while enabling the batched browser path."""
    method = getattr(reader, "lookup_many", None)
    if callable(method):
        try:
            return method(rows)
        except Exception as exc:
            if not is_lookup_timeout(exc):
                raise
            return {clean(row.get("id")): timeout_outcome(exc) for row in rows}
    outcomes = {}
    for row in rows:
        row_id = clean(row.get("id"))
        try:
            outcomes[row_id] = {"detail": reader.lookup(row)}
        except Exception as exc:
            if is_lookup_timeout(exc):
                outcomes[row_id] = timeout_outcome(exc)
            elif isinstance(exc, ValueError):
                outcomes[row_id] = {"error": str(exc)}
            else:
                raise
    return outcomes


@contextmanager
def open_reader(options):
    from playwright.sync_api import sync_playwright
    from bit import bit_zying_caiji as config
    endpoint = config.DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS
    config.ensure_visible_zying_edge_login_window(endpoint, start_url=ORDER_URL)
    if not endpoint.startswith(("http://", "https://", "ws://", "wss://")):
        endpoint = "http://" + endpoint
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=30000)
        page = browser.contexts[0].new_page()
        try:
            page.set_default_timeout(30000)
            page.goto(ORDER_URL, wait_until="domcontentloaded")
            page.bring_to_front()
            yield ZyingPage(page)
        finally:
            page.close()
            # Exiting Playwright disconnects without closing Edge.


class SyncManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self.running = False
        self.active_state = None
        self.login_events = {}

    def _release(self, state):
        state["running"] = False
        if self.active_state is state:
            self.running = False
            self.active_state = None

    def _reap_stalled(self):
        state = self.active_state
        if not state:
            return
        limit = 960 if state.get("phase") == "waiting_login" else SYNC_PROGRESS_TIMEOUT
        if time.time() - state.get("progress_at", state["started_at"]) <= limit:
            return
        state.update(stop_requested=True, phase="stopped", message="同步长时间无进度，已结束旧任务，可重新启动")
        self._release(state)

    def status(self, owner):
        with self.lock:
            self._reap_stalled()
            return copy.deepcopy(self.jobs.get(str(owner), {"running": False, "phase": "idle"}))

    def confirm_login(self, owner):
        with self.lock:
            state = self.jobs.get(str(owner), {})
            if not state.get("running") or state.get("phase") != "waiting_login":
                raise ValueError("当前没有等待登录的同步任务，请重新启动同步")
            state.update(phase="syncing", progress_at=time.time(), message="正在验证登录并同步订单")
            self.login_events[str(owner)].set()
        return self.status(owner)

    def stop(self, owner):
        with self.lock:
            state = self.jobs.get(str(owner), {})
            if state.get("running"):
                state.update(stop_requested=True, phase="stopped", message="任务已结束")
                self.login_events[str(owner)].set()
                self._release(state)
        return self.status(owner)

    def start(self, owner, filters, options, reader, writer, browser_factory=open_reader):
        with self.lock:
            self._reap_stalled()
            if self.running:
                raise ValueError("智赢订单同步正在运行，请稍后再试")
            self.running = True
            state = {"started_at": time.time(), "running": True, "phase": "loading", "total": 0, "completed": 0,
                     "updated": 0, "skipped": 0, "failed": 0, "results": [], "message": "正在获取筛选范围内的全部订单"}
            self.jobs[str(owner)] = state
            self.active_state = state
            login_event = threading.Event()
            self.login_events[str(owner)] = login_event
        def run():
            try:
                rows = snapshot_orders(reader, filters, lambda: state.get("stop_requested"))
                with self.lock:
                    if state.get("stop_requested"):
                        return
                    state.update(total=len(rows), phase="syncing", progress_at=time.time(), message="正在查询智赢采购信息")
                if rows and not state.get("stop_requested"):
                    with browser_factory(options) as browser:
                        if options.get("require_login"):
                            with self.lock:
                                state.update(phase="waiting_login", progress_at=time.time(), message="已打开服务器 Edge，请登录智赢后点击确定登录")
                            if not login_event.wait(900):
                                raise RuntimeError("等待登录超时，请重新启动同步")
                        for start in range(0, len(rows), SYNC_BATCH_SIZE):
                            batch = rows[start:start + SYNC_BATCH_SIZE]
                            if state.get("stop_requested"):
                                break
                            try:
                                outcomes = lookup_many(browser, batch)
                            except Exception as exc:
                                raise ConnectionError(str(exc)) from exc
                            for row in batch:
                                if state.get("stop_requested"):
                                    break
                                result = {"order_id": clean(row["id"]), "order_number": clean(row.get("order_number"))}
                                try:
                                    outcome = outcomes.get(result["order_id"], {})
                                    if outcome.get("skip_reason"):
                                        result.update(status="skipped", message=str(outcome["skip_reason"])[:300])
                                    else:
                                        if outcome.get("error"):
                                            raise ValueError(str(outcome["error"])[:300])
                                        detail = outcome.get("detail")
                                        changes = parse_purchase(detail, row) if detail else None
                                        if changes is None:
                                            result.update(status="skipped", message="未添加采购" if detail else "未找到对应订单")
                                        else:
                                            with self.lock:
                                                if state.get("stop_requested"):
                                                    break
                                                saved = writer(row["id"], changes)
                                            if not saved or int(saved.get("matched") or 0) != 1:
                                                raise ValueError("泽顺订单未成功匹配，未确认回填")
                                            result.update(status="updated", message="已同步", **changes)
                                except Exception as exc:
                                    result.update(status="failed", message=str(exc)[:300])
                                with self.lock:
                                    state[result["status"]] += 1
                                    state["completed"] += 1
                                    state["results"].append(result)
                                    state["progress_at"] = time.time()
                with self.lock:
                    state.update(phase="stopped" if state.get("stop_requested") else "completed",
                                 message="任务已结束" if state.get("stop_requested") else ("同步完成" if rows else "当前筛选没有订单"))
            except Exception as exc:
                with self.lock:
                    state.update(phase="stopped" if state.get("stop_requested") else "failed",
                                 message="任务已结束" if state.get("stop_requested") else str(exc)[:300])
            finally:
                with self.lock:
                    self._release(state)
        thread = threading.Thread(target=run, name="zying-order-sync", daemon=True)
        try:
            thread.start()
        except Exception:
            with self.lock:
                self._release(state)
                state.update(running=False, phase="failed", message="同步线程启动失败")
            raise
        return self.status(owner)


manager = SyncManager()


class AgentSync:
    """Serialize control and writes on the worker; the hub owns Agent lifecycle."""
    def __init__(self):
        self.lock = threading.RLock()
        self.preparing = {}

    def preparing_status(self, owner):
        if owner is None:
            return None
        with self.lock:
            entry = self.preparing.get(str(owner))
            if entry is None:
                return None
            return copy.deepcopy(entry["state"])

    def start_preparing(
        self, owner, agent_id, filters, reader, store, *, required_version,
        created_by_id=None, created_by_name="",
    ):
        """Return immediately while the server gathers the authorized order set."""
        owner_key = str(owner)
        with self.lock:
            current = self.preparing.get(owner_key)
            if current and current["state"].get("running"):
                raise ValueError("智赢订单同步正在准备，请稍后再试")
            state = {
                "started_at": time.time(), "running": True, "phase": "loading",
                "execution_target": "agent", "total": 0, "completed": 0,
                "updated": 0, "skipped": 0, "failed": 0, "results": [],
                "message": "正在后台读取当前筛选范围内的订单",
            }
            entry = {"state": state, "stop_event": threading.Event()}
            self.preparing[owner_key] = entry
            thread = threading.Thread(
                target=self._prepare_and_enqueue,
                args=(owner_key, agent_id, dict(filters), reader, store, entry),
                kwargs={
                    "required_version": required_version,
                    "created_by_id": created_by_id,
                    "created_by_name": created_by_name,
                },
                name="zying-order-sync-prepare", daemon=True,
            )
            entry["thread"] = thread
            try:
                thread.start()
            except Exception:
                self.preparing.pop(owner_key, None)
                raise
            return copy.deepcopy(state)

    def _prepare_and_enqueue(
        self, owner, agent_id, filters, reader, store, entry, *,
        required_version, created_by_id, created_by_name,
    ):
        try:
            rows = snapshot_orders(reader, filters, entry["stop_event"].is_set)
            if entry["stop_event"].is_set():
                raise InterruptedError("任务已结束")
            rows = [
                {key: row.get(key) for key in ("id", "order_number", "pack_id")}
                for row in rows
            ]
            with self.lock:
                if entry["stop_event"].is_set():
                    raise InterruptedError("任务已结束")
                job_id = secrets.token_hex(16)
                version = required_version() if callable(required_version) else required_version
                if isinstance(version, dict):
                    version = version.get("version") or ""
                store.enqueue_job(
                    job_id, agent_id, "zying_order_sync", {"rows": rows},
                    required_version=version,
                    created_by_id=created_by_id, created_by_name=created_by_name,
                )
                if self.preparing.get(owner) is entry:
                    self.preparing.pop(owner, None)
        except InterruptedError as exc:
            with self.lock:
                if self.preparing.get(owner) is entry:
                    entry["state"].update(
                        running=False, phase="stopped", stop_requested=True,
                        message=str(exc),
                    )
        except Exception as exc:
            with self.lock:
                if self.preparing.get(owner) is entry:
                    entry["state"].update(
                        running=False, phase="failed", message=str(exc)[:300],
                    )

    def stop_preparing(self, owner):
        if owner is None:
            return None
        with self.lock:
            entry = self.preparing.get(str(owner))
            if entry is None or not entry["state"].get("running"):
                return None
            entry["stop_event"].set()
            entry["state"].update(
                phase="stopping", stop_requested=True, message="正在停止订单读取",
            )
            return copy.deepcopy(entry["state"])

    def owner_status(self, store, owner, manager):
        job = self.job(store, owner)
        preparing = self.preparing_status(owner)
        if preparing and (
            job is None
            or float(preparing.get("started_at") or 0) >= float(job.get("created_at") or 0)
        ):
            return preparing
        return self.status(job) if job else manager.status(owner)

    def job(self, store, owner):
        if owner is None:
            return None
        with self.lock:
            jobs = store.list_jobs(job_type="zying_order_sync", created_by_id=owner, limit=1)
            job = jobs[0] if jobs else None
            if job and job["status"] in {"running", "stopping"}:
                state = dict(job.get("result") or {})
                now = time.time()
                # Heartbeats renew updated_at even when the browser is stuck.
                # Give legacy jobs without a progress timestamp one grace period.
                progress_at = state.get("progress_at")
                if progress_at is None:
                    state["progress_at"] = now
                    job = store.append_event(job["job_id"], job["agent_id"],
                        status=job["status"], message=job.get("message"), result=state)
                    progress_at = now
                limit = 960 if state.get("phase") == "waiting_login" else SYNC_PROGRESS_TIMEOUT
                if state.get("phase") in {"failed", "stopped", "completed"} or now - progress_at > limit:
                    message = "同步已结束或长时间无进度，已释放旧任务，可重新启动"
                    store.request_cancel(job["job_id"])
                    state.update(phase="stopped", stop_requested=True, message=message)
                    job = store.append_event(job["job_id"], job["agent_id"],
                        status="stopped", message=message, result=state)
        if job and manager.status(owner).get("started_at", 0) > job.get("created_at", 0):
            return None
        return job

    def status(self, job):
        state = dict(job.get("result") or {})
        running = job["status"] in {"queued", "running", "stopping"}
        state.update(running=running, execution_target="agent", task_id=job["job_id"])
        if not running:
            state.update(phase="completed" if job["status"] == "success" else job["status"],
                         message=job.get("message") or "任务已结束")
        elif job.get("cancel_requested"):
            state.update(phase="stopping", message="正在结束任务", stop_requested=True)
        else:
            state.setdefault("message", "等待 Agent 执行")
        return state

    def save(self, store, job, state):
        state["progress_at"] = time.time()
        store.append_event(job["job_id"], job["agent_id"], status="running",
                           message=state.get("message"), result=state)

    def step(self, store, job, data, writer):
        if job.get("cancel_requested") or job["status"] not in {"running"}:
            return {**(job.get("result") or {}), "stop": True}
        state = dict(job.get("result") or {})
        action = data.get("action")
        if action == "ready" and not state.get("phase"):
            state.update(phase="waiting_login", message="已打开 Agent 本机 Edge，请登录智赢后点击确定登录",
                         total=len(job["payload"]["rows"]), completed=0, updated=0, skipped=0, failed=0, results=[])
            self.save(store, job, state)
        elif action in {"row", "rows"}:
            if state.get("phase") != "syncing":
                raise ValueError("请先确认登录")
            entries = ([{"order_id": data.get("order_id"), "detail": data.get("detail"), "error": data.get("error"), "skip_reason": data.get("skip_reason")}]
                       if action == "row" else data.get("rows"))
            if not isinstance(entries, list) or not entries or len(entries) > SYNC_BATCH_SIZE:
                raise ValueError("同步结果批次格式异常")
            rows_by_id = {clean(row.get("id")): row for row in job["payload"]["rows"]}
            normalized = []
            submitted_ids = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("同步结果批次格式异常")
                row_id = clean(entry.get("order_id"))
                row = rows_by_id.get(row_id)
                if row is None:
                    raise ValueError("订单不在本次授权同步范围内")
                if row_id in submitted_ids:
                    raise ValueError("同步结果批次包含重复订单")
                submitted_ids.add(row_id)
                normalized.append((row_id, row, entry))
            completed_ids = {result.get("order_id") for result in state["results"]}
            changed = False
            for row_id, row, entry in normalized:
                if row_id in completed_ids:
                    continue
                result = {"order_id": row_id, "order_number": clean(row.get("order_number"))}
                try:
                    if entry.get("skip_reason"):
                        result.update(status="skipped", message=str(entry["skip_reason"])[:300])
                    else:
                        if entry.get("error"):
                            raise ValueError(str(entry["error"])[:300])
                        detail = entry.get("detail")
                        changes = parse_purchase(detail, row) if detail else None
                        if changes is None:
                            result.update(status="skipped", message="未添加采购" if detail else "未找到对应订单")
                        else:
                            saved = writer(row_id, changes, job)
                            if not saved or int(saved.get("matched") or 0) != 1:
                                raise ValueError("泽顺订单未成功匹配，未确认回填")
                            result.update(status="updated", message="已同步", **changes)
                except Exception as exc:
                    result.update(status="failed", message=str(exc)[:300])
                state[result["status"]] += 1
                state["completed"] += 1
                state["results"].append(result)
                completed_ids.add(row_id)
                changed = True
            if changed:
                self.save(store, job, state)
        return state


agent_sync = AgentSync()


def run_agent_sync(job, stop_event, request_api=None, browser_factory=open_reader):
    if request_api is None:
        from bit.bit_db_api import _request as request_api
    path = "/api/local-agents/zying-sync/" + job["job_id"]
    def call(**data):
        timeout = 180 if data.get("action") in {"row", "rows"} else 60
        return request_api("POST", path, json={**data, "compact": True}, timeout=timeout)
    rows = job["payload"]["rows"]
    state = {}
    if stop_event.is_set():
        return {"phase": "stopped", "message": "任务已结束"}
    if not rows:
        return {"phase": "completed", "message": "当前筛选没有订单"}
    with browser_factory({}) as browser:
        state = call(action="ready")
        deadline = time.monotonic() + 900
        while state.get("phase") != "syncing":
            if state.get("stop") or stop_event.wait(1):
                stop_event.set()
                return state
            if time.monotonic() >= deadline:
                raise RuntimeError("等待登录超时，请重新启动同步")
            state = call(action="control")
        for start in range(0, len(rows), SYNC_BATCH_SIZE):
            # Login and each result response already include the stop state.
            if state.get("stop") or stop_event.is_set():
                stop_event.set()
                return state
            batch = rows[start:start + SYNC_BATCH_SIZE]
            outcomes = lookup_many(browser, batch)
            results = []
            for row in batch:
                result = {"order_id": row["id"]}
                result.update(outcomes.get(clean(row.get("id")), {"error": "未获取到智赢查询结果"}))
                results.append(result)
            if stop_event.is_set():
                return state
            if len(results) == 1:
                result = results[0]
                state = call(action="row", order_id=result["order_id"],
                             **{key: value for key, value in result.items() if key != "order_id"})
            else:
                state = call(action="rows", rows=results)
            if state.get("stop"):
                stop_event.set()
                return state
    state.update(phase="completed", message="同步完成")
    return state
