"""All product workspaces share the right-side detail sheet, offline."""
import pytest

from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390])
def test_product_workspaces_share_drawers_and_keep_list_state(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.evaluate("""() => {
        window.drawerRow = {
            id: 41, title: '不锈钢保温杯', source_item_id: '701168800041',
            source_type: 'zying', review_status: 'approved', ai_status: 'completed',
            net_proceeds_usd: 12, source_weight_g: 250,
            original_1688: {title: '不锈钢保温杯', description_text: '原始描述完整结尾',
                properties: [{name: '材质', value: '不锈钢'}],
                variations: [{sku: 'CUP-BLUE', stock: 10}], price: 20},
            title_es: 'Termo de acero inoxidable', title_pt: 'Garrafa térmica',
            description_es: 'Descripción completa hasta el final',
            description_pt: 'Descrição completa até o final',
            ai_original: {attributes: [{id:'MATERIAL',name:'材质',value_name:'不锈钢'}]}
        };
        products1688Rows = [drawerRow]; products1688RenderRows();
        aiOriginalRows = [drawerRow]; renderAiOriginalProducts();
        mercadoListRows = [drawerRow]; mercadoListMode = 'products'; renderMercadoListRows([drawerRow]);
    }""")
    artifact = ROOT / "artifacts/product-detail-drawers-20261008"
    artifact.mkdir(parents=True, exist_ok=True)

    def assert_sheet(selector, name):
        drawer = page.locator(selector)
        page.wait_for_function("""selector => document.querySelector(selector).getAnimations()
            .every(animation => animation.playState === 'finished')""", arg=selector)
        bounds = drawer.bounding_box()
        assert abs(bounds["x"] + bounds["width"] - width) < 1
        assert bounds["y"] == 0 and bounds["height"] == 1000
        footer = drawer.locator('.p1688-edit-footer, .ai-original-editor-footer, .order-detail-footer')
        if footer.count():
            footer_bounds = footer.bounding_box()
            assert footer_bounds["y"] + footer_bounds["height"] <= 1000
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(artifact / f"{name}-{width}.png"))
        page.keyboard.press("Escape")
        assert not drawer.is_visible()
        assert page.url == "http://console.test/"

    def select_tab(tab):
        page.evaluate("""tab => {
            document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
            document.getElementById('tab-' + tab).classList.add('active');
        }""", tab)

    select_tab("1688-products")
    page.locator('#products-1688-body button').filter(has_text="查看详情").click()
    assert "原始描述完整结尾" in page.locator("#products-1688-source-content").inner_text()
    assert_sheet("#products-1688-detail-dialog", "1688")
    assert page.locator('#products-1688-body button').filter(has_text="修改资料").count() == 0
    assert page.locator('#products-1688-body button').filter(has_text="编辑刊登内容").count() == 0
    assert page.locator('#products-1688-edit-dialog').count() == 0

    select_tab("ai-original-products")
    assert page.locator('#ai-original-grid button').filter(has_text="查看详情").count() == 0
    page.locator('#ai-original-grid button').filter(has_text="编辑分类").click()
    assert "Descripción completa hasta el final" in page.locator('#ai-original-editor-description-es').input_value()
    assert page.locator('#ai-original-editor-variations input[data-variant-field="sku"]').first.input_value() == "CUP-BLUE"
    assert_sheet("#ai-original-editor-dialog", "original-edit")

    select_tab("mercado-products")
    page.evaluate("document.getElementById('tab-mercado-products').appendChild(document.getElementById('mercado-list-module'))")
    assert page.locator('.market-card-detail-trigger, .market-card-detail-content').count() == 0
    assert page.evaluate('typeof openMercadoProductDetail') == 'undefined'
    assert len(page.locator('.market-product-card-row').all()) == 1
    page.locator('.market-product-edit-button').click()
    assert_sheet("#mercado-product-edit-dialog", "products-edit")
    assert not errors
