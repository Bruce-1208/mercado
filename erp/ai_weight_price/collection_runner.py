"""Drive collection checks in visible Edge; business steps stay on the server."""
from .browser import Stopped
from .edge import open_edge


def run(service, step, stop_event):
    current = {}
    task = {}
    try:
        # Shares the normal AWP browser lock, including other processes.
        with service.idle():
            current = step("next", {})
            if current.get("action") == "done":
                return current
            config = service.config.load()
            open_edge(config["cdp_url"], service.store.root)
            with service.browser_factory(config, stop_event, service.store.log) as browser:
                browser.adapt_supplier = lambda snapshot: step("adapt", {"snapshot": snapshot})
                while current.get("action") != "done":
                    if stop_event.is_set():
                        raise Stopped("核重核价已停止")
                    action = current.get("action")
                    if action == "search":
                        task = current["task"]
                        candidates = browser.search_images(task)
                        current = step("search", {"task_id": task["erp_goods_id"], "candidates": candidates})
                        if current.get("action") != "detail":
                            browser.release_search(task)
                    elif action == "detail":
                        detail = browser.read_offer(task, current["candidate"])
                        current = step("detail", {"task_id": task["erp_goods_id"], "detail": detail})
                    else:
                        raise ValueError(f"不支持的采集核查步骤：{action}")
        return current
    except Stopped:
        return step("stop", {})
    except Exception as exc:
        step("fail", {"task_id": task.get("erp_goods_id") or current.get("task", {}).get("erp_goods_id"),
                      "action": current.get("action", "browser"), "error": str(exc)})
        raise
