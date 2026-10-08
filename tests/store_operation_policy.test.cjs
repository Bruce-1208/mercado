const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../bit/templates/index.html'), 'utf8');
const start = template.indexOf('        let storeLinkAuditLoading =');
const end = template.indexOf('        function storeLinkAuditDateBoundary', start);
assert.ok(start >= 0 && end > start);
function element(tag) {
    return {tag, children: [], textContent: '', attributes: {},
        append(...children) {this.children.push(...children);},
        setAttribute(name, value) {this.attributes[name] = value;}};
}
const context = vm.createContext({Date, JSON, document: {createElement: element}});
vm.runInContext(template.slice(start, end), context);

test('dedicated policy filters render readable actions and correct categories', () => {
    const panel = template.slice(template.indexOf('id="tab-store-operations"'), template.indexOf('id="tab-store-links"'));
    assert.match(panel, /<option value="rights_holder">权利人侵权处理<\/option>/);
    assert.match(panel, /<option value="weight_dimensions">重量尺寸变更<\/option>/);
    for (const [row, category, action] of [
        [{action: 'rights_holder_link_result', operation_type: 'rights_holder', phase: 'completed',
          details: {item_id: 'MLM123', status: 'retained', retention_reason: '销量 6 大于 5，保留链接'}}, '权利人侵权处理', 'MLM123 · 销量 6 大于 5，保留链接'],
        [{action: 'weight_dimensions_execution', operation_type: 'weight_dimensions', phase: 'failed',
          details: {entry: {stage: '美客多链接', marketplace_item_id: 'MLM456'}}}, '重量尺寸变更', '美客多链接 · MLM456'],
        [{action: 'process_rights_holder_product', operation_type: 'rights_holder', phase: 'completed',
          details: {result: {title: 'Product'}}}, '权利人侵权处理', '权利人同名产品及链接处理 · Product'],
    ]) {
        const rendered = context.storeLinkAuditRenderRow({...row, created_at: '2026-10-08T00:00:00Z'});
        const task = rendered.children[0].children[1];
        assert.equal(task.children[0].textContent, category);
        assert.equal(task.children[1].textContent, action);
    }
});

test('platform-confirmed deletion has a distinct readable result', () => {
    const summary = context.storeLinkAuditSummary({action: 'rights_holder_link_result',
        created_at: '2026-10-08T00:00:00Z', details: {item_id: 'MLM123', status: 'success'}});
    assert.equal(summary.action, 'MLM123 · 已确认删除');
});
