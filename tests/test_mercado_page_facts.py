"""Regression: native specification tables are not SKU arrays."""
import json
from pathlib import Path

import pytest

from erp.mercadolibre_batch_collector import parse_detail_html, _edge_detail_row, _DETAIL_PAGE_SCRIPT
from erp.mercadolibre_playwright_collector import DETAIL_DOM_SCRIPT
from erp.mercadolibre_page_facts import page_facts, repair_source_facts
from erp.mercadolibre_collection_quality import collection_quality_issues

URL = 'https://articulo.mercadolibre.com.mx/MLM-123456-test'
SECTION = {'title': 'Características principales', 'type': 'TITLE_STRIPED', 'column': 'LEFT',
           'attributes': [{'id': 'Marca', 'text': 'Generic'}, {'id': 'Color', 'text': 'Verde'}]}
VARIANTS = [
    {'id': 101, 'attribute_combinations': [{'id': 'COLOR', 'value_name': 'Verde'}], 'available_quantity': 0, 'price': 120, 'picture_ids': ['green']},
    {'id': 102, 'attribute_combinations': [{'id': 'COLOR', 'value_name': 'Rojo'}], 'price': 130, 'picture_ids': ['red']},
]


def html(variants=True):
    state = {'components': {'specs': {'specs': [SECTION]}, 'recommendations': {'variations': VARIANTS * 5}}}
    if variants:
        state['item'] = {'variations': VARIANTS}
    return '<h1 class="ui-pdp-title">Test</h1><script>window.__PRELOADED_STATE__ = ' + json.dumps(state) + ';</script>'


def test_saved_real_table_sections_reproduce_and_repair_false_missing_attributes():
    source = {'attributes': [{'name': 'Marca', 'value_name': 'Generic'}], 'variations': [SECTION, VARIANTS[0]]}
    repaired = repair_source_facts(source)
    assert repaired['variations'] == [VARIANTS[0]]
    assert repaired['attributes'][0]['value_name'] == 'Generic'
    assert any(a['value_name'] == 'Verde' for a in repaired['attributes'])
    assert repaired['variation_collection_status'] == 'needs_recollection'
    assert source['variations'][0] == SECTION
    issues = collection_quality_issues({'source': source})
    assert not any(i['field'] == 'attributes' for i in issues)
    assert any(i['field'] == 'variations' for i in issues)
    assert repair_source_facts(repaired) == repaired


def test_html_collection_preserves_actual_variants_and_hidden_specs():
    page = parse_detail_html(html(), URL)
    source = _edge_detail_row({'source_item_id': 'MLM123456', 'source_url': URL}, page, {'text': ''})['source']
    assert [v['id'] for v in source['variations']] == [101, 102]
    assert source['variations'][0]['available_quantity'] == 0
    assert source['variations'][1]['available_quantity'] is None
    assert source['variations'][1]['picture_ids'] == ['red']
    assert {a['value_name'] for a in source['attributes']} == {'Generic', 'Verde'}


def test_spec_sections_and_recommendations_do_not_become_variants():
    facts = page_facts(parse_detail_html(html(False), URL))
    assert facts['variations'] == []
    assert len(facts['attributes']) == 2


def test_product_group_variants_and_false_numeric_values():
    facts = page_facts({'structured_scripts': [json.dumps({'@type': 'ProductGroup', 'hasVariant': VARIANTS,
        'additionalProperty': [{'name': 'Rechargeable', 'value': False}, {'name': 'Count', 'value': 0}]})]})
    assert len(facts['variations']) == 2
    assert [a['value_name'] for a in facts['attributes']] == ['False', '0']


def test_broken_json_does_not_discard_dom_specs():
    assert page_facts({'structured_scripts': ['window.__PRELOADED_STATE__ = broken'], 'specs': [{'name': 'Marca', 'value': 'Generic'}]})['attributes'][0]['value_name'] == 'Generic'


@pytest.fixture(scope='module')
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.mark.parametrize('has_variants', [True, False])
def test_browser_extension_and_both_backend_dom_readers(browser, has_variants):
    page = browser.new_page()
    try:
        page.route(URL, lambda route: route.fulfill(body=html(has_variants), content_type='text/html'))
        page.goto(URL)
        page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / 'browser_extension/zeshun_collector/collector-core.js'))
        result = page.evaluate('ZeshunCollectorCore.extractProduct(document, location.href)')
        sources = [result['source'], page_facts(page.evaluate(DETAIL_DOM_SCRIPT)),
                   page_facts(page.evaluate('() => {' + _DETAIL_PAGE_SCRIPT + '}'))]
        for source in sources:
            assert [v['id'] for v in source['variations']] == ([101, 102] if has_variants else [])
            assert {a['value_name'] for a in source['attributes']} == {'Generic', 'Verde'}
            if has_variants:
                assert source['variations'][0]['available_quantity'] == 0
                assert source['variations'][1]['picture_ids'] == ['red']
    finally:
        page.close()


def test_multiple_picker_options_are_flagged_without_inventing_combinations(browser):
    content = '<h1>Test</h1><script type="application/json">' + json.dumps({'components': {'variations': {
        'pickers': [{'id': 'COLOR', 'products': [{'label': 'Red'}, {'label': 'Green'}]}]
    }}}) + '</script>'
    source = page_facts(parse_detail_html(content, URL))
    assert source['variations'] == []
    assert source['variation_collection_status'] == 'incomplete'
    assert any(i['field'] == 'variations' for i in collection_quality_issues({'source': source}))
    page = browser.new_page()
    try:
        page.set_content(content)
        page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / 'browser_extension/zeshun_collector/collector-core.js'))
        extracted = page.evaluate('(url) => ZeshunCollectorCore.extractProduct(document, url)', URL)['source']
        assert extracted['variations'] == []
        assert extracted['variation_collection_status'] == 'incomplete'
    finally:
        page.close()
