"""Real-browser checks of the session-local read-only preload transport."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest

from test_console_ui_browser import console_browser, console_page  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preload_page(console_browser):
    page = console_browser.new_page()
    calls = []
    html = """<!doctype html><html><body>
      <button class="tab-button" data-tab="one">One</button>
      <button class="tab-button" data-tab="two" hidden>Two</button>
      <div class="tab-page active" id="tab-one"></div>
      <div class="tab-page" id="tab-two"></div>
      <script src="/static/console-performance.js"></script>
      </body></html>"""

    def route(r):
        path = urlsplit(r.request.url).path
        if path == "/":
            r.fulfill(content_type="text/html", body=html)
        elif path == "/static/console-performance.js":
            r.fulfill(path=str(ROOT / "bit/static/console-performance.js"))
        else:
            calls.append({"url": r.request.url, "method": r.request.method,
                          "authorization": r.request.headers.get("authorization")})
            r.fulfill(content_type="application/json", body=json.dumps({
                "status": "success", "data": {"request": len(calls), "url": r.request.url},
            }))

    page.route("**/*", route)
    page.goto("http://preload.test/")
    page.evaluate("ZeshunPerformance.configure({modules: [], allowedPaths: ['/api/one', '/api/two']})")
    yield page, calls
    page.close()


def test_warm_response_is_consumed_once_and_refresh_fetches_again(preload_page):
    page, calls = preload_page
    result = page.evaluate("""async () => {
        await ZeshunPerformance.preload('/api/one?page=1');
        const cached = await (await fetch('/api/one?page=1')).json();
        const refreshed = await (await fetch('/api/one?page=1')).json();
        return {cached, refreshed, stats: ZeshunPerformance.snapshot()};
    }""")
    assert len(calls) == 2
    assert result["cached"]["data"]["request"] == 1
    assert result["refreshed"]["data"]["request"] == 2
    assert result["stats"]["hits"] == 1
    assert result["stats"]["cached"] == 0


def test_pending_requests_coalesce_and_each_body_can_be_read(preload_page):
    page, calls = preload_page
    result = page.evaluate("""async () => {
        const replies = await Promise.all([
            fetch('/api/one?b=2&a=1'), fetch('/api/one?a=1&b=2'),
            ZeshunPerformance.preload('/api/one?b=2&a=1')]);
        return Promise.all(replies.map(response => response.json()));
    }""")
    assert len(calls) == 1
    assert [r["data"]["request"] for r in result] == [1, 1, 1]
    assert page.evaluate("ZeshunPerformance.snapshot().cached") == 0


def test_mutation_invalidates_warmed_reads_and_is_never_preloaded(preload_page):
    page, calls = preload_page
    page.evaluate("""async () => {
        await ZeshunPerformance.preload('/api/one');
        await fetch('/api/change', {method: 'POST', body: '{}'});
        await fetch('/api/one');
    }""")
    assert [c["method"] for c in calls] == ["GET", "POST", "GET"]
    assert page.evaluate("ZeshunPerformance.snapshot().hits") == 0


def test_http_200_application_error_is_not_retained(preload_page):
    page, calls = preload_page
    failures = []

    def fail(r):
        failures.append(r.request.url)
        r.fulfill(content_type="application/json", body='{"status":"error","message":"retry"}')

    page.route("**/api/one", fail)
    page.evaluate("""async () => {
        await ZeshunPerformance.preload('/api/one');
        await fetch('/api/one');
    }""")
    assert len(failures) == 2
    assert not calls


def test_expired_preload_and_credentials_are_not_reused(preload_page):
    page, calls = preload_page
    page.evaluate("""async () => {
        await ZeshunPerformance.preload('/api/one');
        const nativeNow = Date.now;
        Date.now = () => nativeNow() + 121000;
        await (await fetch('/api/one')).json();
        Date.now = nativeNow;
        await ZeshunPerformance.preload('/api/one');
        await (await fetch('/api/one', {credentials: 'omit'})).json();
        await (await fetch('/api/one', {headers: {Authorization: 'Bearer isolated-test'}})).json();
    }""")
    assert len(calls) == 5
    assert calls[-1]["authorization"] == "Bearer isolated-test"


def test_scope_change_keeps_organization_data_isolated(preload_page):
    page, calls = preload_page
    result = page.evaluate("""async () => {
        window.auditOrganization = 'first';
        ZeshunPerformance.configure({modules: [], allowedPaths: ['/api/one'], scopeUrl: value => {
            const url = new URL(value, location.href);
            url.searchParams.set('organization', auditOrganization);
            return url.href;
        }});
        await ZeshunPerformance.preload('/api/one');
        auditOrganization = 'second';
        return (await fetch('/api/one?organization=second')).json();
    }""")
    assert len(calls) == 2
    assert "organization=second" in result["data"]["url"]


def test_queue_obeys_permission_hidden_pause_and_concurrency(preload_page):
    page, calls = preload_page
    page.evaluate("""() => {
        window.auditHidden = true;
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => auditHidden});
        ZeshunPerformance.configure({allowedPaths: ['/api/one', '/api/two'], modules: [
            {id: 'one', requests: () => ['/api/one', '/api/one?page=2', '/api/one?page=3']},
            {id: 'two', requests: () => ['/api/two']}
        ]});
    }""")
    page.wait_for_timeout(150)
    assert not calls
    assert page.evaluate("ZeshunPerformance.snapshot().modules") == ["one", "two"]
    assert page.evaluate("ZeshunPerformance.snapshot().queued") == 3
    page.evaluate("auditHidden = false; document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("ZeshunPerformance.snapshot().queued === 0 && ZeshunPerformance.snapshot().running === 0")
    assert len(calls) == 3
    assert all("/api/one" in call["url"] for call in calls)


def test_abort_of_one_subscriber_does_not_cancel_other_subscriber(preload_page):
    page, calls = preload_page
    result = page.evaluate("""async () => {
        const controller = new AbortController();
        const first = fetch('/api/one', {signal: controller.signal}).catch(e => e.name);
        const second = fetch('/api/one').then(r => r.json());
        controller.abort();
        return {first: await first, second: await second};
    }""")
    assert len(calls) == 1
    assert result["first"] == "AbortError"
    assert result["second"]["data"]["request"] == 1


def test_preload_memory_is_bounded_and_oversized_json_is_not_retained(preload_page):
    page, calls = preload_page
    page.evaluate("""async () => {
        for (let i = 0; i < 66; i++) await ZeshunPerformance.preload('/api/one?page=' + i);
    }""")
    stats = page.evaluate("ZeshunPerformance.snapshot()")
    assert len(calls) == 66
    assert stats["cached"] <= 64
    assert stats["bytes"] <= 12 * 1024 * 1024

    page.route("**/api/two", lambda r: r.fulfill(
        content_type="application/json", body=json.dumps({"data": "x" * (3 * 1024 * 1024 + 1)})))
    page.evaluate("ZeshunPerformance.preload('/api/two')")
    assert page.evaluate("ZeshunPerformance.snapshot().cached") == stats["cached"]


def test_every_real_console_module_is_registered(console_page):
    page = console_page
    page.wait_for_function("ZeshunPerformance.snapshot().modules.length > 0")
    coverage = page.evaluate("""() => ({
        pages: [...document.querySelectorAll('.tab-page')].map(p => p.id.slice(4)),
        registered: ZeshunPerformance.snapshot().modules,
        nav: [...document.querySelectorAll('.tab-button[data-tab]')].map(p => p.dataset.tab)
    })""")
    assert len(coverage["registered"]) == len(set(coverage["registered"]))
    template_ids = set(re.findall(r'''\bid=["']tab-([^"']+)["']''',
                                  (ROOT / "bit/templates/index.html").read_text(encoding="utf-8")))
    assert template_ids == set(coverage["registered"])
    # Permission initialization may remove a restricted page from the live DOM.
    assert set(coverage["pages"]) <= set(coverage["registered"])
    assert set(coverage["nav"]) <= set(coverage["registered"])


def test_idle_queue_never_runs_more_than_two_network_reads(preload_page):
    page, calls = preload_page
    held = []
    page.route("**/api/one?held=*", lambda r: held.append(r))
    page.evaluate("""ZeshunPerformance.configure({allowedPaths: ['/api/one'], modules: [
        {id: 'one', requests: () => Array.from({length: 6}, (_, i) => '/api/one?held=' + i)}
    ]})""")
    page.wait_for_function("ZeshunPerformance.snapshot().running === 2")
    page.wait_for_timeout(100)
    assert len(held) == 2
    assert page.evaluate("ZeshunPerformance.snapshot().queued") == 4
    for index in range(6):
        page.wait_for_function("ZeshunPerformance.snapshot().running > 0")
        while len(held) <= index:
            page.wait_for_timeout(20)
        held[index].fulfill(content_type="application/json", body='{"status":"success","data":[]}')
        assert page.evaluate("ZeshunPerformance.snapshot().running") <= 2
    page.wait_for_function("ZeshunPerformance.snapshot().running === 0")
    assert not calls


def test_mutation_does_not_reuse_read_started_before_the_write(preload_page):
    page, calls = preload_page
    held = []
    page.route("**/api/one?held=mutation", lambda r: held.append(r))
    page.evaluate("""() => {
        window.oldWarm = ZeshunPerformance.preload('/api/one?held=mutation').catch(e => e.name);
    }""")
    page.wait_for_function("ZeshunPerformance.snapshot().pending === 1")
    page.evaluate("fetch('/api/change', {method: 'POST', body: '{}'})")
    page.evaluate("""() => {
        window.freshRead = fetch('/api/one?held=mutation').then(r => r.json());
    }""")
    page.wait_for_timeout(100)
    assert len(held) == 2, "A read after a write must not share a pre-write response"
    held[0].fulfill(content_type="application/json", body='{"data":{"version":"old"}}')
    held[1].fulfill(content_type="application/json", body='{"data":{"version":"fresh"}}')
    assert page.evaluate("window.freshRead") == {"data": {"version": "fresh"}}
    assert [c["method"] for c in calls] == ["POST"]


def test_embedded_page_visibility_follows_its_parent_module(preload_page):
    page, _calls = preload_page
    page.evaluate("""() => {
        const frame = document.createElement('iframe');
        frame.id = 'visibility-test-frame';
        frame.srcdoc = '<script src="/static/console-performance.js"><\\/script>';
        document.getElementById('tab-two').append(frame);
    }""")
    page.wait_for_function("document.getElementById('visibility-test-frame').contentWindow.ZeshunPerformance")
    active = "document.getElementById('visibility-test-frame').contentWindow.ZeshunPerformance.isActive()"
    assert page.evaluate(active) is False
    page.evaluate("""() => {
        const child = document.getElementById('visibility-test-frame').contentWindow;
        child.document.addEventListener('zeshun:visibility', event => child.lastVisibility = event.detail.active);
        document.getElementById('tab-two').classList.add('active');
        ZeshunPerformance.notifyVisibility();
    }""")
    assert page.evaluate(active) is True
    assert page.evaluate("document.getElementById('visibility-test-frame').contentWindow.lastVisibility") is True
    page.evaluate("document.getElementById('tab-two').classList.remove('active'); ZeshunPerformance.notifyVisibility()")
    assert page.evaluate(active) is False
    assert page.evaluate("document.getElementById('visibility-test-frame').contentWindow.lastVisibility") is False


@pytest.mark.parametrize("warmed", [False, True])
def test_abort_after_headers_rejects_the_response_body(preload_page, warmed):
    page, calls = preload_page
    result = page.evaluate("""async warmed => {
        if (warmed) await ZeshunPerformance.preload('/api/one');
        const controller = new AbortController();
        const response = await fetch('/api/one', {signal: controller.signal});
        controller.abort();
        return response.json().then(() => 'unexpected success', error => error.name);
    }""", warmed)
    assert result == "AbortError"
    assert len(calls) == 1


def test_body_abort_keeps_the_other_shared_response_readable(preload_page):
    page, calls = preload_page
    result = page.evaluate("""async () => {
        const controller = new AbortController();
        const replies = await Promise.all([
            fetch('/api/one', {signal: controller.signal}), fetch('/api/one')]);
        controller.abort();
        return {aborted: await replies[0].json().catch(error => error.name),
                surviving: await replies[1].json()};
    }""")
    assert result["aborted"] == "AbortError"
    assert result["surviving"]["data"]["request"] == 1
    assert len(calls) == 1


def test_failed_read_does_not_block_idle_queue_when_body_is_unread(preload_page):
    page, calls = preload_page
    page.route("**/api/one?failure=1", lambda r: r.fulfill(
        status=503, content_type="application/json", body='{"status":"error"}'))
    page.evaluate("""async () => {
        try {
            const reply = await fetch('/api/one?failure=1');
            if (!reply.ok) throw new Error('status failed');
        } catch (_) {}
        ZeshunPerformance.configure({allowedPaths: ['/api/two'], modules: [
            {id: 'one', requests: () => ['/api/two']}
        ]});
    }""")
    page.wait_for_function("ZeshunPerformance.snapshot().queued === 0 && ZeshunPerformance.snapshot().running === 0",
                           timeout=3000)
    assert len(calls) == 1
    assert "/api/two" in calls[0]["url"]


def test_hidden_module_status_loaders_emit_no_requests(console_page):
    page = console_page
    calls = page.evaluate("""async () => {
        document.querySelectorAll('.tab-page').forEach(p => p.classList.remove('active'));
        document.getElementById('tab-overview').classList.add('active');
        ZeshunPerformance.notifyVisibility();
        const original = window.fetch, calls = [];
        window.fetch = (url, init) => {
            calls.push(String(url));
            return Promise.resolve(new Response('{"status":"success","data":{}}',
                {headers: {'Content-Type': 'application/json'}}));
        };
        try {
            await Promise.all([
                loadOrderSyncStatus(), loadMercadoPublishStatus(), loadMercadoCollectionStatus(),
                loadApiReputationStatus(), loadAiVideoJobs(true), loadProhibitedListingSyncStatus(),
                loadZyingCollectionStatus(), loadRiskCheckStatus(), loadFundCollectionStatus(),
                loadDailyTaskStatus(), loadStoreLinkSyncStatus(), loadStoreLinkRemoteUpdateStatus(),
                loadMercadoShippingRates()
            ]);
            return calls;
        } finally {window.fetch = original;}
    }""")
    assert calls == []


def test_hidden_browser_pauses_the_active_module_status_reader(console_page):
    page = console_page
    calls = page.evaluate("""async () => {
        Object.defineProperty(document, 'hidden', {configurable: true, value: true});
        document.querySelectorAll('.tab-page').forEach(p => p.classList.remove('active'));
        document.getElementById('tab-orders').classList.add('active');
        ZeshunPerformance.notifyVisibility();
        const original = window.fetch, calls = [];
        window.fetch = (url, init) => {
            calls.push(String(url));
            return Promise.resolve(new Response('{"status":"success","data":{}}',
                {headers: {'Content-Type': 'application/json'}}));
        };
        try {await loadOrderSyncStatus(); return calls;}
        finally {window.fetch = original;}
    }""")
    assert calls == []


def test_status_warm_snapshot_expires_before_list_metadata(preload_page):
    page, calls = preload_page
    result = page.evaluate("""async () => {
        ZeshunPerformance.configure({modules: [], allowedPaths: ['/api/one', '/api/one/status']});
        await ZeshunPerformance.preload('/api/one/status');
        await ZeshunPerformance.preload('/api/one');
        const nativeNow = Date.now;
        Date.now = () => nativeNow() + 6000;
        try {
            const status = await (await fetch('/api/one/status')).json();
            const list = await (await fetch('/api/one')).json();
            return {status, list};
        } finally {Date.now = nativeNow;}
    }""")
    assert len(calls) == 3
    assert result["status"]["data"]["request"] == 3
    assert result["list"]["data"]["request"] == 2


@pytest.mark.parametrize('status,content_type,body', [
    (502, 'text/html', '<!DOCTYPE html><html>Bad Gateway</html>'),
    (200, 'application/json', '<!DOCTYPE html><html>Wrong route</html>'),
    (200, 'application/json', '{broken'),
])
def test_invalid_json_reports_endpoint_without_stream_close_error(preload_page, status, content_type, body):
    page, _ = preload_page
    page.route('**/api/one', lambda r: r.fulfill(status=status, content_type=content_type, body=body))
    result = page.evaluate('''async () => {
        const reply = await fetch('/api/one');
        const clone = reply.clone();
        const errors = await Promise.all([reply, clone].map(r => r.json().catch(e => ({
            name: e.name, message: e.message, status: e.status
        }))));
        return {errors, stats: ZeshunPerformance.snapshot()};
    }''')
    for error in result['errors']:
        assert error['name'] == 'SyntaxError'
        assert '/api/one' in error['message']
        assert f'HTTP {status}' in error['message']
        assert 'close' not in error['message']
        assert error['status'] == status
    assert result['stats']['foregroundReaders'] == 0
    assert result['stats']['pending'] == 0
    assert result['stats']['cached'] == 0
