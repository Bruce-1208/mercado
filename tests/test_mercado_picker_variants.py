"""Picker-backed SKU evidence must survive both offline collectors."""
import json
from pathlib import Path

import pytest

from erp.mercadolibre_batch_collector import parse_detail_html
from erp.mercadolibre_page_facts import page_facts
from erp.mercadolibre_collection_quality import collection_quality_issues
from erp.mercadolibre_batch_collector import _DETAIL_PAGE_SCRIPT
from erp.mercadolibre_playwright_collector import DETAIL_DOM_SCRIPT
from test_mercado_page_facts import browser  # noqa: F401

URL = 'https://articulo.mercadolibre.com.mx/MLM-123456-test'
CORE = Path(__file__).resolve().parents[1] / 'browser_extension/zeshun_collector/collector-core.js'


@pytest.mark.parametrize('container', ['products', 'options', 'values'])
@pytest.mark.parametrize('case', ['explicit', 'single', 'ambiguous', 'partial', 'multidimension'])
def test_picker_variants_backend_and_extension(browser, container, case):
    options = [
        {'variation_id': 101, 'label': 'Red', 'stock': 0, 'price_amount': 120, 'pictureIds': ['red']},
        {'variation_id': 102, 'label': 'Blue', 'stock': 3, 'price_amount': 130, 'pictureIds': ['blue']},
    ]
    if case == 'explicit':
        for row in options:
            row['attributeCombinations'] = [{'id': 'COLOR', 'value': row['label']}]
    elif case == 'ambiguous':
        for row in options:
            row['id'] = row.pop('variation_id')  # Could be a value or another product ID.
    elif case == 'partial':
        options[1].pop('variation_id')
    pickers = [{'id': 'COLOR', container: options}]
    if case == 'multidimension':
        pickers.append({'id': 'SIZE', 'options': [{'label': 'S'}, {'label': 'M'}]})
    state = {'components': {'variations': {'pickers': pickers}}, 'pictures': [
        {'id': color, 'url': f'https://images.test/{color}.jpg'} for color in ['red', 'blue']]}
    content = '<h1>Offline product</h1><script type="application/json">' + json.dumps(state) + '</script>'
    expected = page_facts(parse_detail_html(content, URL))
    page = browser.new_page()
    try:
        page.route('**/*', lambda route: route.abort())
        page.set_content(content)
        page.add_script_tag(path=str(CORE))
        actual = page.evaluate('(url) => ZeshunCollectorCore.extractProduct(document, url).source', URL)
    finally:
        page.close()
    for source in [expected, actual]:
        if case in ('explicit', 'single'):
            assert 'variation_collection_status' not in source
            assert [v['id'] for v in source['variations']] == [101, 102]
            assert [v['available_quantity'] for v in source['variations']] == [0, 3]
            assert [v['price'] for v in source['variations']] == [120, 130]
            assert source['variations'][0]['images'] == ['https://images.test/red.jpg']
            assert source['variations'][1]['attribute_combinations'][0]['value_name'] == 'Blue'
            assert not any(i['field'] == 'variations' for i in collection_quality_issues({'source': source}))
        else:
            assert source['variations'] == []
            assert source['variation_collection_status'] == 'incomplete'


def picker_sources(browser, pickers):
    content = '<h1>Offline product</h1><script type="application/json">' + json.dumps(
        {'components': {'variations': {'pickers': pickers}}}) + '</script>'
    page = browser.new_page()
    try:
        page.route('**/*', lambda route: route.fulfill(body=content, content_type='text/html'))
        page.goto(URL)
        page.add_script_tag(path=str(CORE))
        return [page_facts(parse_detail_html(content, URL)),
                page_facts(page.evaluate(DETAIL_DOM_SCRIPT)),
                page_facts(page.evaluate('() => {' + _DETAIL_PAGE_SCRIPT + '}')),
                page.evaluate('ZeshunCollectorCore.extractProduct(document, location.href).source')]
    finally:
        page.close()


@pytest.mark.parametrize('identity_field', ['variation_id', 'variationId', 'permalink', 'url'])
@pytest.mark.parametrize('label_field', ['label', 'picker_label'])
def test_native_picker_labels_and_explicit_link_identity(browser, identity_field, label_field):
    options = [{label_field: {'text': label}, identity_field:
                (URL + '?variation=' + str(identity) if identity_field in ('url', 'permalink') else identity),
                'stock': stock, 'price_amount': 120 + stock, 'sellerSku': label}
               for identity, label, stock in [(101, 'S', 0), (102, 'M', 3)]]
    for source in picker_sources(browser, [{'picker_id': 'SIZE', 'products': options}]):
        assert 'variation_collection_status' not in source
        assert [str(v['id']) for v in source['variations']] == ['101', '102']
        assert [v['attribute_combinations'] for v in source['variations']] == [
            [{'id': 'SIZE', 'value_name': 'S'}], [{'id': 'SIZE', 'value_name': 'M'}]]
        assert [v['available_quantity'] for v in source['variations']] == [0, 3]
        assert [v['price'] for v in source['variations']] == [120, 123]
        assert [v['seller_sku'] for v in source['variations']] == ['S', 'M']
        assert not any(i['field'] == 'variations' for i in collection_quality_issues({'source': source}))


@pytest.mark.parametrize('identity_field', ['variation_ids', 'variationIds', 'variations'])
def test_dimension_membership_joins_only_existing_skus(browser, identity_field):
    pickers = [
        {'id': 'COLOR', 'options': [
            {'label': {'text': 'Red'}, identity_field: [101, 102], 'stock': 99},
            {'label': {'text': 'Blue'}, identity_field: [103]}]},
        {'id': 'SIZE', 'options': [
            {'label': {'text': 'S'}, identity_field: ['101', '103']},
            {'label': {'text': 'M'}, identity_field: ['102']}]},
    ]
    for source in picker_sources(browser, pickers):
        assert 'variation_collection_status' not in source
        assert [v['id'] for v in source['variations']] == [101, 102, 103]
        assert [[a['value_name'] for a in v['attribute_combinations']] for v in source['variations']] == [
            ['Red', 'S'], ['Red', 'M'], ['Blue', 'S']]
        assert all(v.get('available_quantity') is None for v in source['variations'])


@pytest.mark.parametrize('case', ['missing_dimension', 'conflicting_value', 'unknown_identity', 'foreign_link', 'catalog_id'])
def test_ambiguous_picker_membership_stays_incomplete(browser, case):
    pickers = [
        {'id': 'COLOR', 'options': [{'label': 'Red', 'variation_ids': [101]}, {'label': 'Blue', 'variation_ids': [102]}]},
        {'id': 'SIZE', 'options': [{'label': 'S', 'variation_ids': [101]}, {'label': 'M', 'variation_ids': [102]}]},
    ]
    if case == 'missing_dimension':
        pickers[1]['options'].pop()
    elif case == 'conflicting_value':
        pickers[1]['options'][1]['variation_ids'] = [101, 102]
    elif case == 'unknown_identity':
        pickers[1]['options'][1]['variation_ids'] = [{}]
    else:
        pickers = [{'id': 'SIZE', 'products': [
            {'label': 'S', 'id': 'MLM111', 'permalink': 'https://other.test/item?variation=101'},
            {'label': 'M', 'id': 'MLM222', 'permalink': URL + '?wid=MLM222'}]}]
        if case == 'catalog_id':
            for option in pickers[0]['products']:
                option.pop('permalink')
    for source in picker_sources(browser, pickers):
        assert source['variations'] == []
        assert source['variation_collection_status'] == 'incomplete'


def test_oversized_membership_is_not_treated_as_a_single_sku(browser):
    for source in picker_sources(browser, [{'id': 'COLOR', 'options': [
            {'label': 'Red', 'variation_ids': list(range(1, 502))}]}]):
        assert source['variations'] == []
        assert source['variation_collection_status'] == 'incomplete'
