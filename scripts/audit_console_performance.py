"""Offline browser audit of every ERP tab, with no business-service traffic.

Timing describes local parsing/painting and navigation, not real API latency.
Example: python3 scripts/audit_console_performance.py --output artifacts/console-preload-20261007/after.json
"""
import argparse
from collections import Counter
import gzip
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def render_console(source_root):
    env = Environment(loader=FileSystemLoader(source_root / "bit/templates"))
    return env.get_template("index.html").render(
        current_user={"id": 1, "username": "offline-audit", "display_name": "离线性能审查",
                      "is_admin": True, "is_platform_admin": True, "permissions": ["*"]},
        can_change_task_workers=False, can_confirm_order_outbound=False,
        runtime_role="server", url_for=lambda endpoint, filename: "/static/" + filename,
    )


def payload(path):
    # Empty fixture shapes deliberately contain no real account/product data.
    body = {"status": "success", "success": True, "data": [], "items": [], "rows": [],
            "stores": [], "groups": [], "agents": [], "applications": [], "total": 0,
            "page": 1, "page_size": 50, "pages": 0, "permissions": ["*"], "running": False}
    if path.endswith("/status") or path.endswith("/health") or "authorization" in path:
        body["data"] = {"running": False, "status": "idle", "rows": [], "items": [],
                        "agents": [], "stores": [], "services": [], "applications": []}
    if path == "/api/yandex/console":
        body["data"] = {"running": False, "url": "/offline-frame", "frame_url": "/offline-frame"}
    return body


def browser_path(playwright):
    candidates = [os.environ.get("CONSOLE_TEST_BROWSER", ""), playwright.chromium.executable_path,
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    cache = Path.home() / "Library/Caches/ms-playwright"
    candidates.extend(str(p) for p in sorted(cache.glob("chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"), reverse=True))
    return next((value for value in candidates if value and Path(value).is_file()), None)


def audit(source_root, widths, warm_wait_ms):
    html = render_console(source_root)
    report = {
        "mode": "Offline: real console HTML/static files; every API and embedded document is an empty fixture.",
        "timing_scope": "Local browser parsing, synchronous switchTab cost and two-animation-frame paint wait; excludes production API/network/database latency.",
        "source_root": str(source_root), "rendered_html_bytes": len(html.encode()),
        "rendered_html_gzip_bytes": len(gzip.compress(html.encode(), compresslevel=5, mtime=0)),
        "warm_wait_ms": warm_wait_ms, "viewports": [],
    }
    with sync_playwright() as pw:
        executable = browser_path(pw)
        if not executable:
            raise RuntimeError("Install Chromium or set CONSOLE_TEST_BROWSER")
        browser = pw.chromium.launch(executable_path=executable, headless=True)
        report["browser"] = browser.version
        for width in widths:
            page = browser.new_page(viewport={"width": width, "height": 1000 if width > 760 else 844})
            calls, errors, console_errors = [], [], []

            def route(r):
                path = urlsplit(r.request.url).path
                if path == "/":
                    r.fulfill(body=html, content_type="text/html")
                elif path.startswith("/static/"):
                    file = source_root / "bit" / path.lstrip("/")
                    if file.is_file():
                        r.fulfill(path=str(file))
                    else:
                        r.fulfill(status=404, body="offline asset missing")
                else:
                    calls.append({"method": r.request.method, "path": path,
                                  "url": r.request.url, "type": r.request.resource_type})
                    if r.request.resource_type == "document":
                        r.fulfill(body="<!doctype html><html><body>Offline embedded document fixture</body></html>", content_type="text/html")
                    else:
                        r.fulfill(content_type="application/json", body=json.dumps(payload(path)))

            page.route("**/*", route)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.add_init_script("""window.auditLongTasks = [];
                new PerformanceObserver(list => {
                    for (const e of list.getEntries()) auditLongTasks.push({start: e.startTime, duration: e.duration});
                }).observe({type: 'longtask', buffered: true});""")
            page.goto("http://console-audit.test/", wait_until="load")
            page.wait_for_timeout(warm_wait_ms)
            inventory = page.evaluate("""() => [...document.querySelectorAll('.tab-page')].map(el => {
                const id = el.id.slice(4);
                const nav = document.querySelector('.tab-button[data-tab="' + id + '"]');
                return {id, label: nav?.textContent.trim() || id, navigable: Boolean(nav && !nav.hidden)};
            })""")
            item = page.evaluate("""() => ({width: innerWidth,
                domNodes: document.querySelectorAll('*').length,
                navigation: performance.getEntriesByType('navigation')[0].toJSON(),
                resources: performance.getEntriesByType('resource').map(e => ({
                    name: e.name, decodedBodySize: e.decodedBodySize, duration: e.duration})),
                preload: window.ZeshunPerformance?.snapshot?.() || null,
                duplicateIds: [...new Set([...document.querySelectorAll('[id]')].map(e => e.id))]
                    .filter(id => document.querySelectorAll('[id="' + id + '"]').length > 1)
            })""")
            item["initial_requests"] = dict(Counter(c["path"] for c in calls))
            item["initial_non_read_requests"] = [c for c in calls if c["method"] not in ("GET", "HEAD")]
            item["modules"] = []
            # Explicit Yandex navigation starts its service with POST, which
            # correctly invalidates warm reads. Measure it after other modules.
            for module in sorted(inventory, key=lambda m: m["id"] == "yandex"):
                if not module["navigable"]:
                    item["modules"].append({**module, "reason": "Retained internal page has no available sidebar entry."})
                    continue
                passes = []
                for visit in ("first", "repeat"):
                    start_requests, start_errors = len(calls), len(errors)
                    metrics = page.evaluate("""async id => {
                        const before = window.ZeshunPerformance?.snapshot?.();
                        const start = performance.now();
                        switchTab(id);
                        const synchronousMs = performance.now() - start;
                        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                        const page = document.getElementById('tab-' + id);
                        const after = window.ZeshunPerformance?.snapshot?.();
                        return {synchronousMs, paintWaitMs: performance.now() - start,
                            preloadHits: after && before ? after.hits - before.hits : 0,
                            sharedRequests: after && before ? after.shared - before.shared : 0,
                            active: page.classList.contains('active'),
                            viewportWidth: innerWidth, documentWidth: document.documentElement.scrollWidth,
                            moduleNodes: page.querySelectorAll('*').length};
                    }""", module["id"])
                    page.wait_for_timeout(180)
                    passes.append({"visit": visit, **metrics, "requests": calls[start_requests:], "errors": errors[start_errors:]})
                    page.evaluate("switchTab('overview')")
                    page.wait_for_timeout(30)
                audited_module = {**module, "visits": passes}
                if module["id"] == "inventory":
                    page.evaluate("switchTab('inventory')")
                    subviews = []
                    for view in ("sku", "logs", "shelves"):
                        start_requests, start_errors = len(calls), len(errors)
                        before = page.evaluate("window.ZeshunPerformance?.snapshot?.() || null")
                        metrics = page.evaluate("""async view => {
                            const start = performance.now();
                            switchInventoryView(view);
                            const synchronousMs = performance.now() - start;
                            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                            return {synchronousMs, active: document.getElementById('inventory-view-' + view).classList.contains('active'),
                                documentWidth: document.documentElement.scrollWidth};
                        }""", view)
                        page.wait_for_timeout(180)
                        after = page.evaluate("window.ZeshunPerformance?.snapshot?.() || null")
                        subviews.append({"view": view, **metrics,
                            "preloadHits": after["hits"] - before["hits"] if after and before else 0,
                            "requests": calls[start_requests:], "errors": errors[start_errors:]})
                    audited_module["subviews"] = subviews
                    page.evaluate("switchTab('overview')")
                item["modules"].append(audited_module)
            item["page_errors"] = errors
            item["console_errors"] = console_errors
            item["long_tasks"] = page.evaluate("window.auditLongTasks")
            item["unsafe_requests"] = [call for call in calls if call["method"] not in ("GET", "HEAD")]
            item["total_requests"] = dict(Counter(c["path"] for c in calls))
            report["viewports"].append(item)
            page.close()
        browser.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--widths", type=int, nargs="+", default=[1440, 390])
    parser.add_argument("--warm-wait-ms", type=int, default=3000)
    args = parser.parse_args()
    result = audit(args.source_root.resolve(), args.widths, args.warm_wait_ms)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "viewports": [
        {"width": view["width"], "modules": len(view["modules"]),
         "navigable": sum(m["navigable"] for m in view["modules"]),
         "page_errors": len(view["page_errors"]), "unsafe_requests": len(view["unsafe_requests"])}
        for view in result["viewports"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
