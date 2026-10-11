"""Exercise the real template and pricing workflow offline, without platform writes."""
import json
from urllib.parse import urlsplit, parse_qs
from jinja2 import Environment, FileSystemLoader
import pytest
from bit import price_competition as service
from erp.price_competition_store import PriceCompetitionStore
from test_price_competition import observation, costs
from test_console_ui_browser import ROOT, console_browser  # noqa: F401


def open_center(page, tmp_path, monkeypatch, *, can_manage=True):
    store = PriceCompetitionStore(tmp_path / "pricing.sqlite3")
    rows = [{"id": index, "token_id": 11, "item_id": f"MLM{index}00", "title": title,
             "store_name": "墨西哥旗舰店", "currency_id": "MXN", "price": 100, "is_current": True, "status": "active"}
            for index, title in enumerate(["旅行收纳包 · 轻便款", "无线鼠标 · Catalog 商品", "厨房收纳架 · 暂无参考价", '<img src=x onerror="window.priceXss=1">'], start=1)]
    live = {1: observation(), 2: observation("catalog"), 3: {**observation(), "signal": None}, 4: observation(target=60)}
    for row in rows:
        live[row["id"]]["item_id"] = row["item_id"]
        store.observe(11, row["item_id"], live[row["id"]])
        store.save_costs(11, row["item_id"], costs())
    monkeypatch.setattr(service, "_link", lambda link_id, allowed: rows[link_id - 1])
    monkeypatch.setattr(service, "_inspect", lambda row: live[row["id"]])
    html = Environment(loader=FileSystemLoader(ROOT / "bit/templates")).get_template("price_competition.html").render(
        current_user={"id": 7}, can_manage=can_manage, embedded=True, url_for=lambda endpoint, filename: "/static/" + filename)
    state = {"errors": [], "calls": []}
    page.on("pageerror", lambda error: state["errors"].append(str(error)))
    def route_handler(route):
        request = route.request
        url = urlsplit(request.url)
        state["calls"].append((request.method, url.path, parse_qs(url.query)))
        if url.netloc != "pricing.test":
            return route.abort()
        if url.path == "/price-competition":
            return route.fulfill(body=html, content_type="text/html")
        if url.path.startswith("/static/"):
            return route.fulfill(path=str(ROOT / "bit" / url.path.lstrip("/")))
        if url.path == "/api/price-competition/stores":
            data = [{"id": 11, "name": "墨西哥旗舰店", "enabled": True, "sites": [{"site_id": "MLM"}]}]
        elif url.path == "/api/price-competition":
            data = {"rows": [service._decorate(row, store) for row in rows], "page": 1, "pages": 1, "total": len(rows)}
        else:
            action = url.path.rsplit("/", 1)[-1]
            try:
                data = service.dispatch(action, request.post_data_json, allowed_token_ids={11}, actor="7", store=store)
            except (ValueError, PermissionError) as exc:
                return route.fulfill(status=400, content_type="application/json", body=json.dumps({"status": "error", "message": str(exc)}))
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))
    page.route("**/*", route_handler)
    page.goto("http://pricing.test/price-competition?organization_key=test-company")
    page.locator('#rows tr[data-id="4"]').wait_for()
    return state, store, live


@pytest.mark.parametrize("width", [1440, 390])
def test_separate_scenes_missing_data_profit_block_and_responsive_layout(console_browser, tmp_path, monkeypatch, width):
    page = console_browser.new_page(viewport={"width": width, "height": 1080})
    state, _, _ = open_center(page, tmp_path, monkeypatch)
    assert "官方参考价缺失" in page.locator('#rows tr[data-id="3"]').inner_text()
    assert page.locator('#rows tr[data-id="3"] [data-action="preview"]').is_disabled()
    assert "低于最低可接受价" in page.locator('#rows tr[data-id="4"]').inner_text()
    assert page.locator('#rows tr[data-id="4"] [data-action="preview"]').is_disabled()
    assert page.evaluate("window.priceXss || 0") == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator('[data-kind="catalog"]').click()
    assert page.locator('#rows tr[data-id]').count() == 1
    assert "Full 配送 · 可改善" in page.locator('#rows').inner_text()
    assert "Catalog 使用竞争目标价" in page.locator('#rows').inner_text()
    page.locator('[data-kind="all"]').click()
    artifacts = ROOT / "artifacts/price-competition-ui"
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / f"center-{width}.png"), full_page=True)
    page.locator('#rows tr[data-id="1"] [data-action="costs"]').click()
    page.locator('#fixed-cost').fill("55")
    page.locator('#minimum-profit').fill("10")
    page.locator('#cost-form button[type="submit"]').click()
    page.wait_for_function("!document.getElementById('cost-dialog').open")
    page.locator('#rows tr[data-id="1"] [data-action="preview"]').click()
    page.wait_for_function("document.getElementById('preview-dialog').open")
    assert page.locator('#confirm').is_disabled()
    page.locator('#acknowledged').check()
    page.screenshot(path=str(artifacts / f"confirmation-{width}.png"), full_page=True)
    page.locator('#confirm').click()
    page.wait_for_function("!document.getElementById('preview-dialog').open")
    assert "平台价格未修改" in page.locator('#message').inner_text()
    assert "已确认建议" in page.locator('#rows tr[data-id="1"]').inner_text()
    assert not state["errors"]
    assert all(call[0] in {"GET", "POST"} for call in state["calls"])
    assert all(call[2].get("organization_key") == ["test-company"] for call in state["calls"] if call[1].startswith('/api/'))
    page.close()


def test_changed_official_price_rejects_confirmation_and_keeps_dialog_open(console_browser, tmp_path, monkeypatch):
    page = console_browser.new_page()
    state, _, live = open_center(page, tmp_path, monkeypatch)
    page.locator('#rows tr[data-id="2"] [data-action="preview"]').click()
    page.wait_for_function("document.getElementById('preview-dialog').open")
    live[2]["signal"]["price_to_win"] = 75
    page.locator('#acknowledged').check()
    page.locator('#confirm').click()
    page.locator('#preview-error').wait_for(state="visible")
    assert "已变化" in page.locator('#preview-error').inner_text()
    assert page.locator('#preview-dialog').is_visible()
    assert not state["errors"]
    page.close()


def test_view_only_hides_mutations(console_browser, tmp_path, monkeypatch):
    page = console_browser.new_page()
    state, _, _ = open_center(page, tmp_path, monkeypatch, can_manage=False)
    assert page.locator('[data-action]').count() == 0
    assert page.locator('#refresh-page').count() == 0
    assert not state["errors"]
    page.close()
