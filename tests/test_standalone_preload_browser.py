"""Check iframe lifecycle using real module templates and only offline APIs."""
from collections import Counter
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

from test_console_ui_browser import ROOT, console_browser  # noqa: F401


@pytest.mark.parametrize("module,ip_rights", [("ai_weight_price", False), ("infraction_dashboard", False), ("infraction_dashboard", True)])
def test_preloaded_iframes_keep_initial_reads_and_only_poll_when_active(console_browser, module, ip_rights):
    env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))
    html = env.get_template(module + ".html").render(
        current_user={"role_key": "super_admin", "username": "offline", "is_platform_admin": True},
        embedded=True, local_only=False, can_execute=False, can_configure=False, can_view_all=True,
        agent_launch_available=True, remote_read_only=True, plugin_launch_only=False,
        ip_rights_mode=ip_rights, url_for=lambda endpoint, filename: "/static/" + filename,
    )
    parent = """<style>.tab-page{display:none}.tab-page.active{display:block}</style>
    <section id="module-panel" class="tab-page"><iframe id="module" src="/module"></iframe></section>
    <script src="/static/console-performance.js"></script>"""
    page = console_browser.new_page()
    errors, calls = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # Invoke the actual callbacks deterministically, avoiding a five-minute wait.
    page.add_init_script("""(() => {
        const native = window.setInterval.bind(window); window.testIntervals=[];
        window.setInterval=(callback,delay,...args)=>{
            window.testIntervals.push({callback:()=>callback(...args),delay});
            return native(callback,delay,...args);
        };
    })();""")

    def route(handler):
        request = handler.request
        path = urlsplit(request.url).path
        if path == "/":
            handler.fulfill(body=parent, content_type="text/html")
        elif path == "/module":
            handler.fulfill(body=html, content_type="text/html")
        elif path.startswith("/static/"):
            handler.fulfill(path=str(ROOT / "bit" / path.lstrip("/")))
        else:
            calls.append((request.method, path))
            if path == "/api/ai-weight-price/status":
                handler.fulfill(json={"running": False, "login": {"confirmed": False}, "counts": {},
                    "quota": {"today": 0}, "computer": "offline", "storage": "mock",
                    "execution_identity": {"target": "agent", "agent_id": "offline-agent"}})
            elif path == "/api/ai-weight-price/config":
                handler.fulfill(json={"selectors": {}, "phrases": [], "sku_price_mode": "selected_variant", "daily_limit": 25})
            elif path in {"/api/ai-weight-price/owners", "/api/ai-weight-price/categories"}:
                handler.fulfill(json={"options": [], "meta": {}})
            elif path == "/api/ai-weight-price/logs":
                handler.fulfill(json=[])
            elif path in {"/api/ai-weight-price/tasks", "/api/ai-weight-price/run-items"}:
                handler.fulfill(json={"rows": [], "total": 0})
            elif path == "/api/execution-agents":
                handler.fulfill(json={"status": "success", "data": {"agents": [{"agent_id": "offline-agent", "name": "offline", "online": True}]}})
            else:
                handler.fulfill(json={"status": "success", "data": {"rows": [], "total": 0, "summary": {}, "filters": {}, "running": False}})

    page.route("**/*", route)
    try:
        page.goto("http://module-preload.test/")
        page.wait_for_function("document.querySelector('#module').contentWindow.testIntervals.length >= 2")
        frame = page.frame(url="http://module-preload.test/module")
        if module == "ai_weight_price":
            frame.wait_for_function("document.querySelector('#list-refresh-state').textContent.startsWith('已更新') && !refreshing && !logsBusy")
        else:
            frame.wait_for_function("!state.loading && dashboardLastSyncAt > 0")
        initial = Counter(calls)
        assert initial and all(method == "GET" for method, _ in initial)
        assert frame.evaluate("window.ZeshunPerformance.isActive()") is False
        frame.evaluate("testIntervals.forEach(timer=>timer.callback())")
        assert Counter(calls) == initial
        page.evaluate("document.querySelector('#module-panel').classList.add('active'); ZeshunPerformance.notifyVisibility()")
        assert frame.evaluate("window.ZeshunPerformance.isActive()") is True
        assert Counter(calls) == initial, "Fresh preloaded data should survive the first activation"
        frame.evaluate("testIntervals.forEach(timer=>timer.callback())")
        if module == "ai_weight_price":
            frame.wait_for_function("!statusRefreshBusy && !logsBusy && !awpAgentPromise")
            assert Counter(calls)[("GET", "/api/ai-weight-price/status")] == initial[("GET", "/api/ai-weight-price/status")] + 1
        else:
            frame.wait_for_function("!state.loading && !dashboardSyncPromise")
            assert Counter(calls)[("GET", "/api/official-infractions/sync/status")] == initial[("GET", "/api/official-infractions/sync/status")] + 1
        active = Counter(calls)
        page.evaluate("document.querySelector('#module-panel').classList.remove('active'); ZeshunPerformance.notifyVisibility()")
        frame.evaluate("testIntervals.forEach(timer=>timer.callback())")
        assert Counter(calls) == active
        assert not errors
    finally:
        page.close()
