"""Offline page extraction; no Mercado traffic or appeal submissions."""
from datetime import datetime
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "browser_extension/zeshun_collector/appeal-page.js"


@pytest.fixture(scope="module")
def browser():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).is_file():
            pytest.skip("Chromium is required")
        instance = pw.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    page.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>", content_type="text/html"))
    page.goto("https://global-selling.mercadolibre.com/listings/list")
    page.add_script_tag(path=str(SOURCE))
    yield page
    page.close()


def action(page, command, **args):
    return page.evaluate("([command,args]) => MercadoAppealPage.action(command,args)", [command, args])


@pytest.mark.parametrize("width", [390, 1440])
def test_current_navigation_binds_remote_sites_only_and_switches_with_keyboard(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.set_content('''<details open><summary data-js="site-switcher-target">Country</summary>
      <div class="nav-header-sellers-sites-switcher__option" tabindex="0" role="option" data-value="MLM-remote" data-userid="101" aria-selected="true">Mexico</div>
      <div class="nav-header-sellers-sites-switcher__option" tabindex="0" role="option" data-value="MLB-remote" data-userid="102" aria-selected="false">Brazil</div>
      <div class="nav-header-sellers-sites-switcher__option" data-value="MLM-fulfillment" data-userid="103">Full</div>
      </details><script type="application/json">new win.SiteSwitcherWidget({userId: "100", currentSite: "MLM-remote"});</script>''')
    page.add_script_tag(path=str(SOURCE))
    state = action(page, "context")
    assert state["seller_id"] == "100"
    assert state["site_id"] == "MLM"
    assert state["sites"] == [{"site_id": "MLM", "seller_id": "101"}, {"site_id": "MLB", "seller_id": "102"}]
    page.locator("summary").focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.getAttribute('data-value')") == "MLM-remote"
    assert action(page, "select_site", site_id="MLB")["clicked"] is True


def test_pppi_extracts_bound_titles_rejects_wrong_site_tab_or_partial_page(page):
    now = datetime.now().strftime("%m/%d/%y")
    state = {"title": "Intellectual property - Mexico", "selectedTab": "detections", "detectionsTotal": 1,
             "infractions": {"paging": {"total": 1, "offset": 0, "limit": 7}, "results": [
                 {"date_created": now, "case_id": 900, "element_id": "MLM1234567", "related_filter_name": "FAKES",
                  "item": {"title": "Plain cotton hat"}}]}}
    page.evaluate("state => window.__PRELOADED_STATE__={body:{container:state}}", state)
    result = action(page, "infringement_items", site_id="MLM", offset=0)
    assert result["rows"][0]["title"] == "Plain cotton hat"
    assert result["rows"][0]["eligible"] is True
    assert "站点不匹配" in action(page, "infringement_items", site_id="MLB", offset=0)["error"]
    assert "分页不完整" in action(page, "infringement_items", site_id="MLM", offset=7)["error"]
    page.evaluate("window.__PRELOADED_STATE__.body.container.selectedTab='denounces'")
    assert "error" in action(page, "infringement_items", site_id="MLM", offset=0)


def test_prohibited_list_requires_inactive_filter_and_explicit_reason(page):
    page.set_content('''<button role="tab" aria-selected="true">Inactive</button>
      <article class="sc-list-item" data-item-id="MLM1234567"><h2>Plain hat</h2><p>The product is prohibited.</p></article>
      <article class="sc-list-item" data-item-id="MLM1234568"><h2>Other hat</h2><p>Paused</p></article>''')
    page.add_script_tag(path=str(SOURCE))
    assert action(page, "open_inactive_listings") == {"selected": True}
    result = action(page, "prohibited_items", site_id="MLM")
    assert result["rows"] == [{"product_id": "MLM1234567", "title": "Plain hat", "description": ""}]
    assert result["has_next"] is False
    page.locator("button").evaluate("el=>el.setAttribute('aria-selected','false')")
    assert "尚未加载" in action(page, "prohibited_items", site_id="MLM")["error"]


def test_unknown_page_never_reports_no_products(page):
    assert "error" in action(page, "prohibited_items", site_id="MLM")
    assert "error" in action(page, "infringement_items", site_id="MLM", offset=0)


def test_context_is_available_before_slow_page_resources_finish(page):
    pending = []
    page.route("**/pending-image.png", lambda route: pending.append(route))
    page.route("**/reputation?startup-test=1", lambda route: route.fulfill(content_type="text/html", body='''<script>
      window._n={ctx:{r:{appProps:{pageProps:{userId:'100',siteId:'MLM',nickname:'test-shop'}}}}};
      </script><img src="https://global-selling.mercadolibre.com/pending-image.png">'''))
    page.add_init_script(SOURCE.read_text())
    try:
        page.goto("https://global-selling.mercadolibre.com/reputation?startup-test=1", wait_until="domcontentloaded")
        assert page.evaluate("document.readyState") != "complete"
        state = action(page, "context")
        assert state["seller_id"] == "100"
        assert state["site_id"] == "MLM"
        assert state["logged_out"] is False
    finally:
        for route in pending:
            route.abort()


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("title,button", [
    ("Complaints", "Review metrics"), ("Reclamos", "Ver métricas"),
    ("Reclamações", "Ver em métricas"), ("投诉", "查看详情"),
])
def test_metric_entry_in_outer_footer_stays_with_requested_metric(page, width, title, button):
    page.set_viewport_size({"width": width, "height": 900})
    page.set_content(f'''<main>
      <section><div class="andes-card"><h3 class="variable__title">{title}</h3>
      <span class="variable__percentage">1.2%</span></div>
      <footer><button onclick="window.chosen='complaints'">{button}</button></footer></section>
      <section><h3 class="variable__title">Cancellations</h3>
      <a href="/metrics?type=cancellations">View metrics</a></section></main>''')
    page.add_script_tag(path=str(SOURCE))
    assert action(page, "metric_link", form="投诉") == {"clicked": True}
    assert page.evaluate("window.chosen") == "complaints"


def test_missing_complaint_entry_never_uses_other_metric_or_threshold_zero(page):
    page.set_content('''<main><section><div class="andes-card">
      <h3 class="variable__title">Complaints</h3><span class="variable__percentage">1.2%</span>
      <p>Target 0%</p></div></section>
      <section><h3 class="variable__title">Cancellations</h3>
      <a href="/metrics?type=cancellations">Review in metrics</a></section></main>''')
    page.add_script_tag(path=str(SOURCE))
    assert "订单入口" in action(page, "metric_link", form="投诉")["error"]
    page.locator(".variable__percentage").evaluate("el => el.innerText='0,00%'")
    assert action(page, "metric_link", form="投诉") == {"empty": True}


def test_metric_links_validate_origin_and_ignore_hidden_cards(page):
    page.set_content('''<section hidden><h3 class="variable__title">Complaints</h3>
      <a href="/metrics?old=1">View metrics</a></section>
      <section><h3>Reclamos</h3><a href="/metrics?type=claims">Ver métricas</a></section>''')
    page.add_script_tag(path=str(SOURCE))
    assert action(page, "metric_link", form="投诉") == {
        "url": "https://global-selling.mercadolibre.com/metrics?type=claims"}
    page.locator("section:not([hidden]) a").evaluate("el => el.href='https://example.com/metrics'")
    assert "同源" in action(page, "metric_link", form="投诉")["error"]
