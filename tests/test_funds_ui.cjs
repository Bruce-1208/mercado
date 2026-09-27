const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');
test('fund filters combine salesperson and group, including unassigned groups', () => {
  const group = {value: 'A组'};
  const context = vm.createContext({
    fundOwnerFilter: {value: '张三'},
    document: {getElementById: () => group},
    fundRows: [
      {'店铺归属人': '张三', '店铺组': 'A组', '店铺名': '甲'},
      {'店铺归属人': '张三', '店铺组': 'B组', '店铺名': '乙'},
      {'店铺归属人': '李四', '店铺组': 'A组', '店铺名': '丙'},
      {'店铺归属人': '张三', '店铺名': '丁'},
    ],
  });
  vm.runInContext(html.slice(html.indexOf('        function fundRowOwner'), html.indexOf('        function visibleFundShops')), context);
  assert.equal(vm.runInContext('visibleFundRows().map(r => r["店铺名"]).join() ', context), '甲');
  group.value = '未分组';
  assert.equal(vm.runInContext('visibleFundRows()[0]["店铺名"]', context), '丁');
  context.fundOwnerFilter.value = '';
  group.value = 'A组';
  assert.equal(vm.runInContext('visibleFundRows().length', context), 2);
});
