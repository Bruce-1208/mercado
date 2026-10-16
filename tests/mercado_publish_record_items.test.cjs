const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const template = fs.readFileSync('bit/templates/index.html', 'utf8');
const copies = [];
const context = vm.createContext({
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    window: {copyTextReliably: (...args) => copies.push(args)},
});
vm.runInContext(template.slice(template.indexOf('        function mercadoPublishRecordItems(row)'), template.indexOf('        function renderMercadoPublishRecords(data)')), context);
const items = row => JSON.parse(JSON.stringify(context.mercadoPublishRecordItems(row)));

test('shows the destination listing instead of the reusable global product ID', () => {
    const row = {site_id: 'MLM', published_item_id: 'U123', result_json: JSON.stringify({result: {
        siteless_user_product_id: 'U123',
        site_items: [{site_id: 'MLB', item_id: 'MLB777'}, {site_id: 'MLM', item_id: 'MLM-12345'}],
    }})};
    assert.deepEqual(items(row), [{id: 'MLM12345', url: 'https://articulo.mercadolibre.com.mx/MLM-12345'}]);
    const html = context.renderMercadoPublishRecordItems(row);
    assert.match(html, /target="_blank" rel="noopener noreferrer"/);
    assert.match(html, /data-copy-ignore data-published-item-id="MLM12345"/);
    assert.match(html, /oncontextmenu="copyMercadoPublishRecordItem/);
    assert.doesNotMatch(html, /U123|MLB777/);
});

test('deduplicates variants and excludes failed and other-site listings', () => {
    assert.deepEqual(items({site_id: 'MLB', result_json: {result: {
        site_items: [{item_id: 'MLB1'}, {item_id: 'MLB2', error: {message: 'failed'}}],
        user_products: [{site_items: [{item_id: 'MLB1'}, {item_id: 'MLB3'}, {item_id: 'MLM4'}]}],
    }}}), [
        {id: 'MLB1', url: 'https://produto.mercadolivre.com.br/MLB-1'},
        {id: 'MLB3', url: 'https://produto.mercadolivre.com.br/MLB-3'},
    ]);
});

test('legacy local IDs link to the correct marketplace even with malformed results', () => {
    const hosts = {MLM: 'articulo.mercadolibre.com.mx', MLB: 'produto.mercadolivre.com.br',
        MLA: 'articulo.mercadolibre.com.ar', MLC: 'articulo.mercadolibre.cl',
        MCO: 'articulo.mercadolibre.com.co', MLU: 'articulo.mercadolibre.com.uy'};
    for (const [site_id, host] of Object.entries(hosts)) {
        assert.deepEqual(items({site_id, published_item_id: `${site_id}999`, result_json: '{broken'}),
            [{id: `${site_id}999`, url: `https://${host}/${site_id}-999`}]);
    }
});

test('global IDs remain copyable without inventing a storefront URL', () => {
    const html = context.renderMercadoPublishRecordItems({published_item_id: 'CBTU123'});
    assert.match(html, /暂未返回站点产品编号/);
    assert.doesNotMatch(html, /href=/);
    assert.deepEqual(items({published_item_id: 'javascript:alert(1)'}), []);
    assert.equal(context.renderMercadoPublishRecordItems({}), '<span class="market-list-summary">-</span>');
});

test('right click copies only the product ID and prevents navigation', () => {
    let prevented = false;
    let stopped = false;
    context.copyMercadoPublishRecordItem({preventDefault: () => {prevented = true;},
        stopPropagation: () => {stopped = true;}}, {dataset: {publishedItemId: 'MLM12345'}});
    assert.equal(prevented, true);
    assert.equal(stopped, true);
    assert.equal(copies.at(-1)[0], 'MLM12345');
    assert.equal(copies.at(-1)[1].message, '产品编号已复制');
});

test('recovers successful global IDs from array responses when the stored scalar ID is empty', () => {
    const row = {site_id: 'MLM', published_item_id: '', result_json: {result: [
        {id: 'CBT123'}, {id: 'CBT456'}, {id: 'CBT123'}, {id: 'CBT789', error: 'failed'},
    ]}};
    assert.deepEqual(items(row), [{id: 'CBT123', url: ''}, {id: 'CBT456', url: ''}]);
    const html = context.renderMercadoPublishRecordItems(row);
    assert.match(html, /<details class="publish-record-details publish-record-items">/);
    assert.match(html, /共 2 个编号/);
    assert.doesNotMatch(html, / open|href=/);
});

test('reads nested response wrappers but never picks up source or request product IDs', () => {
    const row = {site_id: 'MLM', result_json: {source_item_id: 'MLM999', payload: {id: 'MLM888'},
        result: {raw_response: [{result: {site_items: [{item_id: 'MLM123'}, {item_id: 'MLM456'}]}}]}}};
    assert.deepEqual(items(row).map(item => item.id), ['MLM123', 'MLM456']);
    assert.match(context.renderMercadoPublishRecordItems(row), /共 2 个编号/);
    assert.doesNotMatch(context.renderMercadoPublishRecordItems({published_item_id: 'MLM123'}), /<details/);
});
