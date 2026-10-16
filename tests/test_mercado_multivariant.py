"""Offline collection -> saved snapshot -> Mercado publication contracts."""
import copy
import json
from pathlib import Path

import pytest

from erp.mercadolibre_batch_collector import parse_detail_html, _edge_detail_row
from erp.mercadolibre_batch_publish import _prepared_listing_from_product_row
from erp.mercadolibre_collection_store import _editor_variations
from erp.mercadolibre_page_facts import page_facts
from erp.mercadolibre_source_store import normalize_snapshot
from erp import mercadolibre_follow_sell as publish
from test_erp_mercadolibre_follow_sell import FamilyClient, sample_source
from test_mercado_page_facts import browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://articulo.mercadolibre.com.mx/MLM-3016972321-test'


def product_state():
    rows = {}
    for index, (color, size) in enumerate([('Red', 'S'), ('Red', 'M'), ('Blue', 'S'), ('Blue', 'M')]):
        row = {'sellerSku': f'{color}-{size}', 'price_amount': 120 + index,
               'attributeCombinations': [{'attribute_id': 'COLOR', 'value': {'name': color}},
                                         {'id': 'SIZE', 'value': size}],
               'pictureIds': [color]}
        if index < 3:
            row['availableQuantity'] = [0, 3, 7][index]
        rows[str(100 + index)] = row
    return {'item': {'skuMap': rows, 'pictures': [
        {'id': color, 'secure_url': f'https://images.test/{color}.jpg'} for color in ('Red', 'Blue')
    ]}}


def markup(state):
    return '<h1 class="ui-pdp-title">Producto de prueba</h1><script type="application/json">' + json.dumps(state) + '</script>'


def collected_source(state=None):
    page = parse_detail_html(markup(state or product_state()), URL)
    row = _edge_detail_row({'source_item_id': 'MLM3016972321', 'source_url': URL}, page, {'text': ''})
    source = {**sample_source(), 'variations': row['source']['variations']}
    source['attributes'].append({'id': 'COLOR', 'value_name': 'Red'})
    return source


class MatrixClient(FamilyClient):
    def __init__(self, account='family'):
        super().__init__()
        self.account = account

    def request(self, method, path, **kwargs):
        if path == '/users/me':
            return {'id': 77, 'site_id': 'MLM' if self.account == 'local' else 'CBT',
                    'tags': ['user_product_seller'] if self.account == 'family' else []}
        if method == 'POST':
            self.posts.append((path, copy.deepcopy(kwargs['json_body'])))
            if self.account == 'family':
                return [{'siteless_user_product_id': f'U{i}', 'siteless_family_id': 99,
                         'site_items': [{'site_id': 'MLM', 'item_id': f'MLM{i}'}]} for i in range(4)]
            return {'id': 'MLM999' if self.account == 'local' else 'CBT999'}
        result = super().request(method, path, **kwargs)
        if path in ('/categories/CBT301/attributes', '/categories/MLM301/attributes'):
            return result + [{'id': 'SIZE'}]
        return result


@pytest.mark.parametrize('account', ['family', 'global', 'local'])
def test_collected_matrix_survives_storage_preparation_and_mock_publication(account):
    source = collected_source()
    record = normalize_snapshot({'item_id': source['id'], 'source_url': URL, 'source': source})
    assert len(record['variations']) == 4
    assert [v['id'] for v in record['variations']] == ['100', '101', '102', '103']
    snapshot = {'source': record['source'], 'description': {'plain_text': 'Description'}}
    row = {'id': 1, 'source_type': 'collected', 'source_item_id': source['id'], 'source_url': URL,
           'source_snapshot_json': json.dumps(snapshot)}
    prepared = _prepared_listing_from_product_row(row, 'MLM')
    before = copy.deepcopy(prepared)
    client = MatrixClient(account)
    result = publish.follow_sell(client, URL, prepared_listing=prepared, quantity=9,
                                 local_price=199, net_proceeds=20, publish=True)
    assert prepared == before
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    if account == 'family':
        assert path == '/global/user-products/families'
        rows = payload
        assert len(client.uploads) == 2  # Reuse a color image across sizes.
        assert len({r['family_name'] for r in rows}) == 1
        assert len(result['result']['site_items']) == 4
    else:
        rows = payload['variations'] if account == 'local' else payload['sites_to_sell'][0]['variations']
        gallery = payload['pictures'] if account == 'local' else payload['sites_to_sell'][0]['pictures']
        assert {url for v in rows for url in v['picture_ids']} <= {p['source'] for p in gallery}
        assert not any(a['id'] in ('COLOR', 'SIZE') for a in payload['attributes'])
        if account == 'local':
            assert path == '/items'
            assert [v['price'] for v in rows] == [199] * 4
    assert len(rows) == 4
    assert [v['available_quantity'] for v in rows] == [0, 3, 7, 9]
    assert [next(a['value_name'] for a in v['attributes'] if a['id'] == 'SELLER_SKU') for v in rows] == ['Red-S', 'Red-M', 'Blue-S', 'Blue-M']


@pytest.mark.parametrize('status', ['incomplete', 'needs_recollection'])
@pytest.mark.parametrize('account', ['family', 'global', 'local'])
def test_incomplete_collection_never_posts_or_uploads(status, account):
    client = MatrixClient(account)
    source = sample_source()  # Empty variants must not silently become a single SKU.
    source['variation_collection_status'] = status
    with pytest.raises(publish.MercadoLibreError, match='规格未采全'):
        publish.follow_sell(client, URL, prepared_listing=(source, {}), net_proceeds=20, publish=True)
    assert client.posts == client.uploads == []


def test_url_picture_references_and_unresolved_images():
    source = collected_source()
    source['variations'][0]['picture_ids'] = ['https://images.test/direct.jpg']
    source['variations'][0].pop('images')
    payload = publish.build_local_payload(source, {})
    assert payload['variations'][0]['picture_ids'] == ['https://images.test/direct.jpg']
    source['variations'][0]['picture_ids'] = ['unknown-picture']
    with pytest.raises(publish.MercadoLibreError, match='图片无法解析'):
        publish.build_local_payload(source, {})


def test_jsonld_actual_variants_preserve_dimensions_sku_and_offer_price():
    state = {'@type': 'ProductGroup', 'hasVariant': [
        {'@type': 'Product', 'sku': f'SKU-{size}', 'color': 'Red', 'size': size,
         'image': [f'https://images.test/{size}.jpg'], 'offers': {'price': 120}}
        for size in ['S', 'M']]}
    source = collected_source(state)
    assert [v['seller_sku'] for v in source['variations']] == ['SKU-S', 'SKU-M']
    assert [v['price'] for v in source['variations']] == [120, 120]
    payload = publish.build_local_payload(source, {})
    assert payload['variations'][1]['attribute_combinations'][-1]['value_name'] == 'M'
    assert payload['variations'][1]['picture_ids'] == ['https://images.test/M.jpg']


@pytest.mark.parametrize('count', [4, 201])
def test_extension_and_backend_preserve_same_variants_and_image_mapping(browser, count):
    state = product_state()
    if count > 4:
        state['item']['skuMap'] = {str(i): {**state['item']['skuMap']['100'], 'sellerSku': f'SKU-{i}'} for i in range(count)}
    content = markup(state)
    expected = page_facts(parse_detail_html(content, URL))['variations']
    page = browser.new_page()
    try:
        page.route('**/*', lambda route: route.abort())
        page.set_content(content)
        page.add_script_tag(path=str(ROOT / 'browser_extension/zeshun_collector/collector-core.js'))
        actual = page.evaluate('(url) => ZeshunCollectorCore.extractProduct(document, url).source.variations', URL)
        assert len(actual) == len(expected) == count
        for left, right in zip(actual, expected):
            for key in ('id', 'seller_sku', 'attribute_combinations', 'picture_ids', 'images', 'price'):
                assert left[key] == right[key]
            assert left.get('available_quantity') == right.get('available_quantity')
        assert len(_editor_variations(actual)) == count
    finally:
        page.close()


@pytest.mark.parametrize('case', ['partial', 'overflow'])
def test_partial_picker_or_oversized_matrix_cannot_become_single_sku(browser, case):
    state = product_state()
    if case == 'partial':
        state['item']['skuMap'] = {'100': state['item']['skuMap']['100']}
        state['pickers'] = [{'id': 'COLOR', 'options': ['Red', 'Blue']}]
    else:
        state['item']['skuMap'] = {str(i): state['item']['skuMap']['100'] for i in range(501)}
    content = markup(state)
    assert page_facts(parse_detail_html(content, URL))['variation_collection_status'] == 'incomplete'
    page = browser.new_page()
    try:
        page.route('**/*', lambda route: route.abort())
        page.set_content(content)
        page.add_script_tag(path=str(ROOT / 'browser_extension/zeshun_collector/collector-core.js'))
        assert page.evaluate('(url) => ZeshunCollectorCore.extractProduct(document, url).source.variation_collection_status', URL) == 'incomplete'
    finally:
        page.close()


def test_editor_refuses_oversized_matrix_instead_of_truncating():
    with pytest.raises(ValueError, match='500'):
        _editor_variations([{}] * 501)


def test_clearing_collected_stock_does_not_restore_hidden_alias():
    source = collected_source()
    assert 'availableQuantity' not in source['variations'][0]
    source['variations'][0].pop('available_quantity')
    payload = publish.build_local_payload(source, {}, quantity=9)
    assert payload['variations'][0]['available_quantity'] == 9


def test_one_unresolved_picture_is_not_silently_dropped():
    source = collected_source()
    source['variations'][0]['picture_ids'].append('missing')
    with pytest.raises(publish.MercadoLibreError, match='图片无法解析'):
        publish.build_global_payload(MatrixClient(), source, {}, net_proceeds=20)
