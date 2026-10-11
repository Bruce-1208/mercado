const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('bit/templates/index.html', 'utf8');
const context = vm.createContext({window: {mercadoInfringementPublishIssues: row => row.risk ? ['侵权风险'] : []}});
vm.runInContext(html.slice(html.indexOf('        function mercadoProductUsesManualReview('),
    html.indexOf('        function mercadoSelectedValues(')), context);
for (const source of ['zying', 'collected', 'pulled']) {
    test(`${source}: approved selection reaches publishing despite missing collection metadata`, () => {
        const row = {source_type: source, review_status: 'approved', weight_g: null, net_proceeds_usd: null};
        assert.equal(context.mercadoProductPublishIssues(row).length, 0);
        assert.deepEqual(Array.from(context.mercadoProductPublishIssues({...row, review_status: 'unreviewed'})), ['审核状态未通过']);
        assert.deepEqual(Array.from(context.mercadoProductPublishIssues({...row, risk: true})), ['侵权风险']);
    });
}
test('other sources keep existing preparation checks', () => {
    assert.equal(context.mercadoProductDataPublishIssues({source_type: 'ai_original', review_status: 'approved'}).length, 2);
});
