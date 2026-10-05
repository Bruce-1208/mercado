const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/mercado_action_center.html'), 'utf8');
const source = template.slice(template.indexOf('    function beijingDate('), template.indexOf('    function statusLabel('));
for (const timezone of ['UTC', 'America/Mexico_City', 'Asia/Shanghai']) {
    test(`deadlines and editor stay in Beijing time with browser timezone ${timezone}`, () => {
        const before = process.env.TZ;
        try {
            process.env.TZ = timezone;
            const context = vm.createContext({});
            vm.runInContext(source, context);
            assert.equal(context.localInput('2026-10-03T18:00:00-04:00'), '2026-10-04T06:00');
            assert.equal(context.localInput('2026-10-04 06:00:00'), '2026-10-04T06:00');
            assert.equal(context.localInput('2026-10-03T22:00:00Z'), '2026-10-04T06:00');
            assert.match(context.dateLabel('2026-10-03T22:00:00Z'), /06:00:00.*北京时间/);
            assert.equal(context.localInput(''), '');
            assert.equal(context.dateLabel(null), '未设置');
        } finally {
            if (before === undefined) delete process.env.TZ;
            else process.env.TZ = before;
        }
    });
}
