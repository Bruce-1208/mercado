const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {chrome: {runtime: {onMessage: {addListener() {}}}}, getComputedStyle: () => ({display: 'block', visibility: 'visible'})};
vm.createContext(context);
vm.runInContext(fs.readFileSync('browser_extension/zeshun_collector/content-zying.js', 'utf8'), context);

test('dimension readback compares all three numeric sides', () => {
  assert.equal(context.aiWeightPriceSame('dimensions_cm', '30.0×20×10', '30x20x10'), true);
  for (const actual of ['30x20x11', '30x20', '0x20x10', 'NaNx20x10']) {
    assert.equal(context.aiWeightPriceSame('dimensions_cm', actual, '30x20x10'), false);
  }
});

test('named dimensions keep length width height order and reject missing controls', () => {
  const nodes = ['sizeLength', 'sizeWidth', 'sizeHeight'].map(id => ({id, getClientRects: () => [1]}));
  const root = {querySelectorAll: selector => nodes.filter(node => selector.includes(`id="${node.id}"`))};
  assert.equal(context.aiWeightPriceDimensionControls(root)[0], nodes[0]);
  assert.equal(context.aiWeightPriceDimensionControls(root)[2], nodes[2]);
  nodes.pop();
  assert.throws(() => context.aiWeightPriceDimensionControls(root), /未能分别唯一定位/);
});
