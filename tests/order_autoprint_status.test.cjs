const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('bit/templates/index.html', 'utf8');
const source = html.slice(html.indexOf('        function renderOrderSyncStatus('), html.indexOf('        async function loadOrderSyncStatus('));
function render(data) {
 const context = vm.createContext({
  ordersSyncRunning:false, orderAutoPrintFinishedAt:'', orderAutoState:{},
  document:{getElementById:()=>({})}, orderSyncFooterStatus:{}, orderSyncLog:{},
  orderSyncStartButton:{}, orderSyncStartDate:{}, orderSyncEndDate:{}, orderSyncStoreRows:[],
  orderSyncStoreList:{querySelectorAll:()=>[]},orderSyncDialog:{querySelectorAll:()=>[]},loadOrders:()=>{},
 });
 vm.runInContext(source, context);
 context.renderOrderSyncStatus(data);
 return context.orderAutoState;
}
test('automatic print errors remain visible after sync completes', () => {
 const state = render({auto_print:{status:'partial',results:[{status:'failed',message:'面单暂不可用'}]}});
 assert.match(state.textContent,/自动打印未完成：面单暂不可用/);
 assert.match(state.className,/error/);
});
test('successful round restores normal schedule', () => {
 const state = render({auto_print:{status:'completed'},next_run_at:'2026-10-10 15:00:00'});
 assert.match(state.textContent,/15:00/);
 assert.doesNotMatch(state.className,/error/);
});
