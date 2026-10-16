const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('bit/templates/index.html', 'utf8');
const helper = source.slice(source.indexOf('        async function readMercadoPublishResponse'), source.indexOf('        async function loadMercadoPublishStatus'));
const context = vm.createContext({});
vm.runInContext(helper, context);
for (const status of [200, 500, 502, 503, 504]) {
    test(`HTML response ${status} has readable diagnostic`, async () => {
        await assert.rejects(context.readMercadoPublishResponse(new Response('<!doctype html><title>Error</title>', {status})), new RegExp(`HTTP ${status}`));
    });
}
test('JSON business errors and success are preserved', async () => {
    for (const payload of [{status:'success', data:{running:true}}, {status:'error', message:'数据库暂时不可用'}]) {
        const result = await context.readMercadoPublishResponse(new Response(JSON.stringify(payload)));
        assert.equal(JSON.stringify(result), JSON.stringify(payload));
    }
});
test('invalid JSON shapes are rejected', async () => {
    for (const body of ['null', '[]', '123']) {
        await assert.rejects(context.readMercadoPublishResponse(new Response(body)), /响应格式异常/);
    }
});
