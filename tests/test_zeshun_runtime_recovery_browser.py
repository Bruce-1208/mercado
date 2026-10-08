"""Offline regression checks for content scripts left behind by a reload."""
import json
import os
import shutil
from pathlib import Path

import pytest

from test_1688_product_management_browser import products_browser


EXTENSION = Path(__file__).resolve().parents[1] / "browser_extension/zeshun_collector"
REFRESH_HINT = "插件连接已失效，请刷新当前网页后重试"


@pytest.mark.parametrize("failure", ["missing", "invalidated"])
def test_preferences_explain_lost_runtime_and_can_retry(products_browser, failure):
    page = products_browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content("<html><body>1688</body></html>")
    page.evaluate("""() => {
      window.calls = [];
      window.workingRuntime = {
        onMessage: {addListener() {}},
        sendMessage: async message => {
          calls.push(message.type);
          return message.type === 'GET_STATE' ? {authenticated:true}
            : {ok:true,categories:[{id:7,name:'家居'}]};
        }
      };
      window.chrome = {runtime:workingRuntime, storage:{local:{
        get:async()=>({}),set:async()=>{}
      }}};
    }""")
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    panel = page.locator(".zeshun-1688-preferences")
    page.wait_for_function("!document.querySelector('.zeshun-1688-preferences select').disabled")
    page.evaluate("""failure => {
      chrome.runtime = failure === 'missing' ? undefined : {
        sendMessage() {throw new Error('Extension context invalidated.');}
      };
    }""", failure)
    panel.locator("button").click()
    assert REFRESH_HINT in panel.inner_text()
    assert panel.locator("select").is_disabled()
    assert page.evaluate("calls") == ["GET_STATE", "GET_ZESHUN_CATEGORIES"]
    page.evaluate("chrome.runtime = workingRuntime")
    panel.locator("button").click()
    page.wait_for_function("!document.querySelector('.zeshun-1688-preferences select').disabled")
    assert "泽顺 1688 采集分类" in panel.inner_text()
    assert errors == []
    page.close()


def test_real_extension_reload_reinjects_and_refreshes_old_page(products_browser, tmp_path):
    # Use the production content scripts with a local-only worker: no accounts,
    # credentials, remote API calls or business jobs are involved in this test.
    fixture = tmp_path / "extension"
    fixture.mkdir()
    for name in ("content-1688.js", "launcher.js"):
        shutil.copyfile(EXTENSION / name, fixture / name)
    shutil.copytree(EXTENSION / "icons", fixture / "icons")
    (fixture / "manifest.json").write_text(json.dumps({
        "manifest_version": 3, "name": "Zeshun reload regression", "version": "1.0.0",
        "permissions": ["storage", "tabs", "scripting"],
        "host_permissions": ["https://s.1688.com/*"],
        "background": {"service_worker": "worker.js"},
        "content_scripts": [{"matches": ["https://s.1688.com/*"],
                             "js": ["launcher.js", "content-1688.js"]}],
    }), encoding="utf-8")
    (fixture / "worker.js").write_text("""
      globalThis.received = [];
      chrome.runtime.onInstalled.addListener(() => {});
      chrome.runtime.onMessage.addListener((message, _sender, respond) => {
        received.push(message.type);
        respond(message.type === 'GET_STATE' ? {authenticated:true}
          : {ok:true,categories:[{id:7,name:'Home'}]});
      });
    """, encoding="utf-8")
    context = products_browser.browser_type.launch_persistent_context(
        str(tmp_path / "profile"), headless=True,
        executable_path=os.environ.get("CONSOLE_TEST_BROWSER") or products_browser.browser_type.executable_path,
        ignore_default_args=["--disable-extensions"],
        args=[f"--disable-extensions-except={fixture}", f"--load-extension={fixture}"],
    )
    try:
        # Match an unpacked installation made by a user. With developer mode
        # off Chromium disables a command-line extension when it reloads.
        admin = context.new_page()
        admin.goto("chrome://extensions")
        developer_mode = admin.locator("extensions-toolbar #devMode")
        if not developer_mode.evaluate("element => element.checked"):
            developer_mode.click()
        assert developer_mode.evaluate("element => element.checked")
        admin.close()
        context.route("https://s.1688.com/**", lambda route: route.fulfill(
            body="<html><body>Offline 1688</body></html>", content_type="text/html"))
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://s.1688.com/selloffer/offer_search.htm")
        panel = page.locator(".zeshun-1688-preferences")
        page.wait_for_function("document.querySelector('.zeshun-1688-preferences select')?.disabled === false")
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
        # Schedule after evaluate has returned, so closing the worker does not
        # turn the test driver's evaluation into an unrelated transport error.
        closed = []
        worker.on("close", lambda _: closed.append(True))
        with context.expect_event("serviceworker", timeout=10000) as restarted:
            worker.evaluate("setTimeout(() => chrome.runtime.reload(), 0)")
        worker = restarted.value
        assert worker.evaluate("chrome.runtime.onMessage.hasListeners()")
        for _ in range(100):
            if closed:
                break
            page.wait_for_timeout(50)
        assert closed, "The test extension must reload before exercising the old page"
        panel.locator("button").click()
        assert REFRESH_HINT in panel.inner_text()
        assert panel.locator("select").is_disabled()
        launcher = page.locator("#zeshun-plugin-launcher button")
        launcher.click()
        assert launcher.locator("span").inner_text() == "请刷新"
        assert REFRESH_HINT in launcher.get_attribute("title")
        assert errors == []

        # A background job may inject the new content script before the user
        # refreshes. Its controls must replace those bound to the old context.
        worker.evaluate("""async () => {
          const [tab] = await chrome.tabs.query({url:'https://s.1688.com/*'});
          await chrome.scripting.executeScript({target:{tabId:tab.id}, files:['content-1688.js']});
        }""")
        page.wait_for_function("document.querySelector('.zeshun-1688-preferences select')?.disabled === false")
        assert page.locator('.zeshun-1688-preferences').count() == 1
        before_focus = worker.evaluate("received.length")
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        page.wait_for_function("document.querySelector('.zeshun-1688-preferences select')?.disabled === false")
        assert worker.evaluate("received.length") == before_focus + 2
        page.evaluate("""document.body.insertAdjacentHTML('beforeend',
          '<div class="search-offer-wrapper"><a title="Offline product" href="https://detail.1688.com/offer/1234567.html">Offline product</a></div>')""")
        page.locator('.zeshun-card-collect').click()
        page.wait_for_function("document.querySelector('.zeshun-card-collect').textContent.includes('已采集')")
        assert worker.evaluate("received.filter(type => type === 'SUBMIT_PRODUCT').length") == 1

        page.reload()
        page.wait_for_function("document.querySelector('.zeshun-1688-preferences select')?.disabled === false")
        assert "泽顺 1688 采集分类" in panel.inner_text()
        assert launcher.locator("span").inner_text() == "泽顺"
        assert errors == []
    finally:
        context.close()
