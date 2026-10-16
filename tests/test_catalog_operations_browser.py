"""Offline ownership controls for the four catalog workspaces."""
import pytest
from test_console_ui_browser import console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
def test_only_owned_products_can_be_selected_and_edited(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    for mode in ['1688', 'original', 'collection', 'products']:
        result = page.evaluate('''mode => {
            const rows = [true, false].map((can_operate, i) => ({
                id: i + 1, can_operate, title: '模拟产品', source_type: 'zying',
                source_item_id: '70116880000' + i, visibility: 'enterprise',
                original_1688: {title: '模拟产品'}, ai_original: {},
            }));
            let root, selection, tab;
            if (mode === '1688') {
                products1688Rows = rows; products1688Selected.clear();
                products1688RenderRows(); products1688ToggleAll(true);
                root = document.querySelector('.p1688-editable-row').parentElement;
                selection = [...products1688Selected]; tab = '1688-products';
            } else if (mode === 'original') {
                aiOriginalRows = rows; aiOriginalSelected.clear();
                renderAiOriginalProducts(); toggleAllAiOriginalProducts(true);
                root = document.querySelector('.ai-original-card').parentElement;
                selection = [...aiOriginalSelected]; tab = 'ai-original-products';
            } else {
                mercadoListMode = mode; mercadoListRows = rows;
                selectedMercadoCollectionIds.clear(); renderMercadoListRows(rows);
                toggleAllMercadoItems(true); root = mercadoListBody;
                selection = [...selectedMercadoCollectionIds];
                tab = mode === 'collection' ? 'mercado-collection' : 'mercado-products';
            }
            document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
            document.getElementById('tab-' + tab)?.classList.add('active');
            const checks = [...root.querySelectorAll('input[type=checkbox]')];
            return {selection, disabled: checks.map(el => el.disabled)};
        }''', mode)
        assert result['selection'] == [1]
        assert result['disabled'] == [False, True]
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors


def test_catalog_permissions_exist_before_inline_list_initialization(console_browser):
    """Inline list startup must never outrun the shared permission renderer."""
    import json
    import re
    from urllib.parse import urlsplit
    from jinja2 import Environment, FileSystemLoader
    from test_console_ui_browser import ROOT

    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('index.html').render(
        current_user={'username': 'preview', 'display_name': '界面预览', 'is_admin': True},
        can_change_task_workers=False, runtime_role='server',
        url_for=lambda endpoint, filename: '/static/' + filename)
    probe = '''<script>
      window.catalogBootstrap = {type: typeof window.catalogOperationMarkup};
      if (window.catalogBootstrap.type === 'function') {
        window.catalogBootstrap.markup = window.catalogOperationMarkup(
          {can_operate:false}, '<button onclick="editProduct()">编辑</button>');
      }
    </script>'''
    html, count = re.subn(r'(<script\b[^>]*src="[^"\n]*product-visibility\.js[^"\n]*"[^>]*></script>)',
                         lambda match: match[0] + probe, html)
    assert count == 1
    page = console_browser.new_page()
    def route(request):
        path = urlsplit(request.request.url).path
        if path == '/':
            request.fulfill(body=html, content_type='text/html')
        elif path.startswith('/static/'):
            request.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
        else:
            request.fulfill(content_type='application/json', body=json.dumps(
                {'success': True, 'data': [], 'items': [], 'stores': [], 'groups': [], 'total': 0}))
    page.route('**/*', route)
    try:
        page.goto('http://console.test/')
        state = page.evaluate('window.catalogBootstrap')
        assert state['type'] == 'function'
        assert 'disabled' in state['markup']
    finally:
        page.close()
