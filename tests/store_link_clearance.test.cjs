const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync('bit/templates/index.html', 'utf8');
const context = vm.createContext({escapeHtml: s => String(s).replaceAll('"','&quot;').replaceAll('<','&lt;')});
vm.runInContext(template.slice(template.indexOf('        function storeLinkMarkers(row)'), template.indexOf('        function storeLinkPromotionDetails(')), context);
test('clearance participation is distinct from ordinary promotion and unknown data', () => {
  assert.match(context.storeLinkMarkers({group_name:'半托管',clearance_status:'enrolled'}), /已参加清仓/);
  assert.match(context.storeLinkMarkers({group_name:'半托管',clearance_status:'not_enrolled'}), /未参加清仓/);
  const candidate = context.storeLinkMarkers({group_name:'半托管',promotion_applied:true, clearance_status:'not_enrolled'});
  assert.match(candidate,/已报活动/);
  assert.doesNotMatch(candidate,/已参加清仓/);
  assert.match(context.storeLinkMarkers({group_name:'半托管',}), /清仓待同步/);
  assert.doesNotMatch(context.storeLinkMarkers({group_name:'半托管',}), /未参加清仓/);
  assert.doesNotMatch(context.storeLinkMarkers({group_name:'自发货',clearance_status:'not_enrolled'}), /清仓/);
});
