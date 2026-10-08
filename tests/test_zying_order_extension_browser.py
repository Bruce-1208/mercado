"""Exercise the extension's MAIN-world reader and date-only popup offline."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from test_zying_order_sync_speed import order_page

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension/zeshun_collector"
PAGE_SCRIPT = (EXTENSION / "zying-order-page.js").read_text()


@pytest.mark.parametrize("fallback", [False, True])
def test_page_search_details_concurrency_and_compact_payload(fallback):
    with order_page(count=10) as (reader, calls):
        page = reader.page
        # Synthetic key events invoke the SPA's Enter handler, rather than
        # Chromium's native form default (which requires a trusted event).
        page.evaluate("() => {document.querySelector('input').onkeydown = e => {if(e.key === 'Enter') document.querySelector('form').requestSubmit();};}")
        if fallback:
            page.evaluate("performance.clearResourceTimings()")
        page.add_script_tag(content=PAGE_SCRIPT)
        outcome = page.evaluate("rows => zeshunReadZyingOrderBatch(rows)", [{"id": str(2000000 + i)} for i in range(10)])
        assert not outcome.get("error"), outcome
        assert len(outcome["rows"]) == 10
        assert len(calls) == 10
        assert page.evaluate("window.stats.peak") == (1 if fallback else 4)
        for index, result in enumerate(outcome["rows"]):
            assert result["order_id"] == str(2000000 + index)
            assert result["detail"]["root"][0]["order_id"] == str(100 + index)
            assert "customer" not in result["detail"]["root"][0]
            assert set(result["detail"]) == {"root", "buys"}
        assert page.evaluate("window.fetch.name") != "wrappedFetch"


def test_page_rejects_other_order_detail_and_reports_login_expiry():
    with order_page(count=2, wrong_id=999) as (reader, _):
        page = reader.page
        page.evaluate("() => {document.querySelector('input').onkeydown = e => {if(e.key === 'Enter') document.querySelector('form').requestSubmit();};}")
        page.add_script_tag(content=PAGE_SCRIPT)
        outcome = page.evaluate("rows => zeshunReadZyingOrderBatch(rows)", [{"id": "2000000"}, {"id": "2000001"}])
        assert "其他订单" in outcome["rows"][0]["error"]
        assert outcome["rows"][1]["detail"]["root"][0]["order_id"] == "101"
    with order_page(count=2, rejected_id=100) as (reader, _):
        page = reader.page
        page.evaluate("() => {document.querySelector('input').onkeydown = e => {if(e.key === 'Enter') document.querySelector('form').requestSubmit();};}")
        page.add_script_tag(content=PAGE_SCRIPT)
        outcome = page.evaluate("rows => zeshunReadZyingOrderBatch(rows)", [{"id": "2000000"}, {"id": "2000001"}])
        assert outcome["login_required"] is True
        assert page.evaluate("window.stats.active") == 0


@pytest.mark.parametrize("control", ["text", "icon", "missing"])
def test_search_clicks_control_after_model_update_without_enter(control):
    with order_page(count=1) as (reader, _):
        page = reader.page
        page.evaluate("""control => {
            const field = document.querySelector('input');
            const form = field.form;
            const submit = form.onsubmit;
            form.onsubmit = e => e.preventDefault();
            window.searchClicks = 0;
            window.enterEvents = 0;
            field.onkeydown = () => window.enterEvents++;
            field.oninput = () => setTimeout(() => window.searchModel = field.value, 0);
            if (control === 'missing') {
                form.replaceWith(field);
                return;
            }
            const button = document.createElement('button');
            button.type = 'button';
            button.innerHTML = control === 'text' ? '订单搜索' : '<span class="ant-input-search-icon" aria-label="search"></span>';
            button.onclick = event => {
                if (window.searchModel !== field.value) throw new Error('stale model');
                window.searchClicks++;
                submit(event);
            };
            form.append(button);
        }""", control)
        page.add_script_tag(content=PAGE_SCRIPT)
        outcome = page.evaluate("rows => zeshunReadZyingOrderBatch(rows)", [{"id": "2000000"}])
        if control == "missing":
            assert "搜索按钮" in outcome["error"]
            assert "rows" not in outcome
        else:
            assert outcome["rows"][0]["detail"]["root"][0]["order_id"] == "100"
            assert page.evaluate("window.searchClicks") == 1
        assert page.evaluate("window.enterEvents") == 0
        assert page.evaluate("window.fetch.name") != "wrappedFetch"


def test_click_without_request_stops_instead_of_skipping_orders():
    with order_page(count=1) as (reader, _):
        page = reader.page
        page.evaluate("""() => {
            const button = document.createElement('button');
            button.type = 'button'; button.textContent = '订单搜索';
            document.querySelector('form').append(button);
            const original = window.setTimeout;
            window.setTimeout = (fn, delay, ...args) => original(fn, delay === 30000 ? 100 : delay, ...args);
        }""")
        page.add_script_tag(content=PAGE_SCRIPT)
        outcome = page.evaluate("rows => zeshunReadZyingOrderBatch(rows)", [{"id": "2000000"}])
        assert "未发起查询" in outcome["error"]
        assert "rows" not in outcome
        assert page.evaluate("window.fetch.name") != "wrappedFetch"


def test_popup_dates_login_progress_and_stop():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("https://extension.test/**", lambda route: route.fulfill(path=str(EXTENSION / route.request.url.rsplit("/", 1)[-1])))
        page.add_init_script("""
          window.messages = [];
          window.orderState = {phase:'idle',running:false};
          window.chrome = {
            tabs:{query:async()=>[]}, storage:{local:{get:async()=>({}),set:async()=>{}}},
            runtime:{openOptionsPage:()=>{},sendMessage:(message,callback)=>{
              messages.push(message);
              if(message.type==='GET_STATE') return callback({ok:true,authenticated:true,user:{username:'test'},queueLength:0});
              if(message.type==='GET_ZESHUN_CATEGORIES') return callback({ok:true,rows:[]});
              if(message.type==='START_ZYING_ORDER_SYNC') orderState={request_id:'run1',phase:'loading',running:true,
                date_start:message.date_start,date_end:message.date_end,total:12,prepared:10,message:'正在准备订单，可先登录智赢'};
              if(message.type==='CONFIRM_ZYING_ORDER_LOGIN') orderState={...orderState,phase:'syncing',message:'正在同步',completed:10,updated:8,skipped:1,failed:1};
              if(message.type==='STOP_ZYING_ORDER_SYNC') orderState={...orderState,phase:'stopped',running:false,message:'任务已结束'};
              callback({ok:true,state:structuredClone(orderState)});
            }}
          };
        """)
        page.goto("https://extension.test/popup.html")
        page.locator("#zying-order-mode").click()
        page.wait_for_function("!document.getElementById('zying-order-start').disabled")
        page.locator("#zying-order-date-start").fill("2026-10-09")
        page.locator("#zying-order-date-end").fill("2026-10-08")
        page.locator("#zying-order-start").click()
        assert "有效" in page.locator("#zying-order-status").inner_text()
        assert not page.evaluate("messages.some(m => m.type === 'START_ZYING_ORDER_SYNC')")
        page.locator("#zying-order-date-start").fill("2026-10-01")
        page.locator("#zying-order-start").click()
        page.wait_for_function("document.getElementById('zying-order-summary').textContent.includes('准备订单 10 / 12')")
        assert page.locator("#zying-order-confirm").is_disabled()
        assert page.locator("#zying-order-stop").is_enabled()
        assert page.locator("#zying-order-open").is_visible()
        page.evaluate("orderState={...orderState,phase:'waiting_login',message:'请登录智赢'}")
        page.wait_for_function("!document.getElementById('zying-order-confirm').disabled")
        assert page.locator("#zying-order-date-start").is_disabled()
        assert page.evaluate("messages.find(m => m.type === 'START_ZYING_ORDER_SYNC')") == {
            "type": "START_ZYING_ORDER_SYNC", "date_start": "2026-10-01", "date_end": "2026-10-08"}
        page.locator("#zying-order-confirm").click()
        page.wait_for_function("document.getElementById('zying-order-summary').textContent.includes('10 / 12')")
        artifact = ROOT / "artifacts/zying-order-extension-20261008"
        artifact.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact / "popup.png"), full_page=True)
        page.locator("#zying-order-stop").click()
        page.wait_for_function("!document.getElementById('zying-order-start').disabled")
        assert page.locator("#zying-order-confirm").is_disabled()
        assert page.locator("#zying-order-date-start").is_enabled()
        assert not errors, errors
        browser.close()
