"""Exercise the official-module fast path in a real offline browser."""
import json
import time
from contextlib import contextmanager

import pytest
from playwright.sync_api import sync_playwright

from bit import zying_order_sync as sync


@contextmanager
def order_page(count=10, *, wrong_id=None, timeout_id=None, rejected_id=None):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        calls = []
        rows = [{"id": str(100 + i), "no": str(2000000 + i)} for i in range(count)]
        module = r"""
            export const api = {getFreeStyleApi: async (command, body, server, config = {}) => {
                window.stats ||= {active: 0, peak: 0, calls: [], aborted: 0};
                const stats = window.stats;
                stats.active++;
                stats.peak = Math.max(stats.peak, stats.active);
                stats.calls.push({command, id: body.id});
                try {
                    await new Promise((resolve, reject) => {
                        const delay = body.id === TIMEOUT_ID ? 10000 : 100 + (110 - body.id) * 5;
                        const onAbort = () => {
                            clearTimeout(timer); stats.aborted++;
                            reject(new Error('canceled'));
                        };
                        const timer = setTimeout(() => {
                            config.signal?.removeEventListener('abort', onAbort);
                            resolve();
                        }, delay);
                        config.signal?.addEventListener('abort', onAbort, {once: true});
                    });
                    const response = await fetch('/api/CmdHandler?cmd=' + command, {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body), signal: config.signal
                    });
                    return await response.json();
                } finally { stats.active--; }
            }};
        """.replace("TIMEOUT_ID", json.dumps(timeout_id))

        def route_request(route):
            request = route.request
            if request.url.endswith("/official-api.js"):
                route.fulfill(content_type="text/javascript", body=module)
            elif "cmd=orders.load" in request.url:
                route.fulfill(json={"code": 200, "data": {"list": {"data": rows, "maxcount": count}}})
            elif "cmd=order.view" in request.url:
                internal_id = request.post_data_json["id"]
                calls.append(internal_id)
                if internal_id == rejected_id:
                    route.fulfill(json={"code": 401, "message": "登录失效"})
                else:
                    route.fulfill(json={"code": 200, "data": {
                        "root": [{"order_id": str(wrong_id if wrong_id is not None and internal_id == 100 else internal_id),
                                  "order_no": str(2000000 + internal_id - 100), "order_cost": "18.50",
                                  "customer": "unused private data"}],
                        "buys": [{"no": "P" + str(internal_id), "trace": "T" + str(internal_id), "amount": "18.50", "cur": "CNY", "other": "x"}],
                        "items": [{"large_unused_data": "x" * 10000}]
                    }})
            else:
                route.fulfill(content_type="text/html; charset=utf-8", body=f"""
                    <meta charset="utf-8"><form><input placeholder="{sync.SEARCH_PLACEHOLDER}"></form>
                    <table><tbody></tbody></table>
                    <script type="module">
                    import {{api}} from '/official-api.js';
                    document.querySelector('form').onsubmit = async event => {{
                        event.preventDefault();
                        const response = await fetch('/api/CmdHandler?cmd=orders.load', {{method:'POST',
                            headers:{{'Content-Type':'application/json'}},
                            body:JSON.stringify({{key:document.querySelector('input').value}})}});
                        const rows = (await response.json()).data.list.data;
                        document.querySelector('tbody').innerHTML = rows.map(row =>
                            `<tr data-row-key="${{row.id}}"><td>${{row.no}}</td></tr>`).join('');
                        document.querySelectorAll('tr').forEach(row => row.onclick = () =>
                            api.getFreeStyleApi('order.view', {{id:Number(row.dataset.rowKey)}}));
                    }};
                    </script>
                """)

        page.route("**/*", route_request)
        page.goto(sync.ORDER_URL, wait_until="networkidle")
        try:
            yield sync.ZyingPage(page), calls
        finally:
            browser.close()


def test_bounded_concurrency_keeps_out_of_order_details_and_compacts_payload():
    with order_page() as (reader, calls):
        outcomes = reader.lookup_many([{"id": str(2000000 + i)} for i in range(10)])
        stats = reader.page.evaluate("window.stats")
        assert stats["peak"] == sync.SYNC_DETAIL_CONCURRENCY
        assert stats["active"] == 0
        assert len(calls) == 10
        assert calls[:4] != sorted(calls[:4])  # delayed responses complete out of order
        assert all(call["command"] == "order.view" for call in stats["calls"])
        for i in range(10):
            data = outcomes[str(2000000 + i)]["detail"]
            assert data["root"][0]["order_id"] == str(100 + i)
            assert sync.parse_purchase(data, {"id": str(2000000 + i)}) == {
                "purchase_order": "P" + str(100 + i), "purchase_tracking": "T" + str(100 + i),
                "purchase_cost": "18.50"}
            assert set(data) == {"root", "buys"}
            assert "customer" not in data["root"][0]
            assert "other" not in data["buys"][0]


def test_fast_path_rejects_mismatched_internal_id_without_losing_other_orders():
    with order_page(count=2, wrong_id=999) as (reader, calls):
        outcomes = reader.lookup_many([{"id": "2000000"}, {"id": "2000001"}])
        assert "error" in outcomes["2000000"]
        assert outcomes["2000001"]["detail"]["root"][0]["order_id"] == "101"


def test_fast_path_timeout_aborts_request_and_continues(monkeypatch):
    monkeypatch.setattr(sync, "SYNC_LOOKUP_TIMEOUT_MS", 250)
    with order_page(count=5, timeout_id=101) as (reader, calls):
        outcomes = reader.lookup_many([{"id": str(2000000 + i)} for i in range(5)])
        assert "超时" in outcomes["2000001"]["skip_reason"]
        assert all(outcomes[str(2000000 + i)].get("detail") for i in (0, 2, 3, 4))
        stats = reader.page.evaluate("window.stats")
        assert stats["aborted"] == 1
        assert stats["active"] == 0
        assert stats["peak"] <= sync.SYNC_DETAIL_CONCURRENCY


def test_fast_path_authentication_failure_stops_sync():
    with order_page(count=2, rejected_id=101) as (reader, calls):
        with pytest.raises(RuntimeError, match="登录状态"):
            reader.lookup_many([{"id": "2000000"}, {"id": "2000001"}])


def test_fast_path_is_faster_than_serial_ui_with_equal_response_delays():
    durations = {}
    for mode in ("serial", "parallel"):
        with order_page() as (reader, calls):
            if mode == "serial":
                reader._api_available = False
            started = time.monotonic()
            outcomes = reader.lookup_many([{"id": str(2000000 + i)} for i in range(10)])
            durations[mode] = time.monotonic() - started
            assert all(value.get("detail") for value in outcomes.values())
    assert durations["parallel"] < durations["serial"] * 0.7, durations
