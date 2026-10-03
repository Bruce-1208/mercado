'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const catalog = require('../bit/static/size-chart-presets.js');

test('shared library covers clothing, children, footwear, accessories and custom specifications', () => {
  const expected = [
    'adult_tops', 'adult_coats', 'adult_pants', 'adult_jeans', 'dresses', 'skirts', 'suits',
    'sportswear', 'swimwear', 'bras', 'underwear', 'baby_clothing', 'kids_clothing',
    'adult_footwear', 'kids_footwear', 'baby_footwear', 'socks', 'hats', 'gloves', 'belts',
    'rings', 'bags_luggage', 'home_textiles', 'general_products'
  ];
  assert.ok(catalog.version);
  assert.equal(catalog.presets.length, expected.length);
  for (const id of expected) assert.equal(catalog.getPreset(id)?.id, id);
  assert.equal(catalog.getPreset('missing'), null);
  assert.equal(catalog.getPreset('__proto__'), null);
  assert.equal(catalog.getSizeSystem('adult_tops', 'missing'), null);
  assert.equal(catalog.getSizeSystem('missing', 'alpha'), null);
});

test('all catalog IDs and labels are unique, finite, and usable as editable suggestions', () => {
  assert.equal(new Set(catalog.presets.map(item => item.id)).size, catalog.presets.length);
  for (const item of catalog.presets) {
    assert.match(item.id, /^[a-z0-9_]+$/);
    assert.ok(item.name && item.group && item.description);
    assert.ok(['BODY_MEASURE', 'CLOTHING_MEASURE', 'MIXED_MEASURE'].includes(item.measure_type));
    assert.ok(Array.isArray(item.domain_hints));
    assert.ok(item.domain_hints.every(hint => typeof hint === 'string' && hint.trim()));
    assert.ok(item.measurements.length > 0);
    assert.equal(new Set(item.measurements.map(measurement => measurement.name)).size, item.measurements.length);
    assert.ok(item.size_systems.length >= 2);
    assert.equal(new Set(item.size_systems.map(system => system.id)).size, item.size_systems.length);
    for (const system of item.size_systems) {
      assert.match(system.id, /^[a-z0-9_]+$/);
      assert.ok(system.name && system.labels.length > 0 && system.labels.length <= 100);
      assert.ok(system.labels.every(label => typeof label === 'string' && label.trim() === label && label.length > 0 && label.length <= 120));
      assert.equal(new Set(system.labels.map(label => label.toLowerCase())).size, system.labels.length);
      assert.equal(catalog.getSizeSystem(item.id, system.id), system);
    }
  }
});

test('regional and specialized labels remain separate rather than paired conversions', () => {
  assert.ok(catalog.getSizeSystem('adult_tops', 'alpha').labels.includes('M'));
  assert.ok(catalog.getSizeSystem('adult_tops', 'eu_women').labels.includes('38'));
  assert.ok(catalog.getSizeSystem('adult_tops', 'us_women').labels.includes('8'));
  for (const id of ['eu', 'us_women', 'us_men', 'uk', 'br']) assert.ok(catalog.getSizeSystem('adult_footwear', id));
  assert.ok(catalog.getSizeSystem('kids_footwear', 'us_child').labels.includes('13C'));
  assert.ok(catalog.getSizeSystem('kids_footwear', 'us_youth').labels.includes('1Y'));
  assert.ok(catalog.getSizeSystem('adult_jeans', 'waist_inseam_label').labels.includes('W28 L30'));
  assert.ok(catalog.getSizeSystem('bras', 'us_band_cup').labels.includes('34DDD'));
  assert.ok(catalog.getSizeSystem('bras', 'uk_band_cup').labels.includes('34DD'));
  assert.ok(catalog.getSizeSystem('baby_clothing', 'month_label'));
  assert.ok(catalog.getSizeSystem('baby_clothing', 'height_label'));
  for (const id of ['us', 'eu', 'hk', 'jp']) assert.ok(catalog.getSizeSystem('rings', id));
  for (const item of catalog.presets) {
    for (const system of item.size_systems) assert.deepEqual(Object.keys(system).sort(), ['id', 'labels', 'name']);
  }
});

test('catalog does not fabricate official fields, seller/domain IDs or real measurements', () => {
  const forbidden = new Set(['domain_id', 'attribute_id', 'value_id', 'seller_id', 'token_id', 'site_id', 'main_attribute', 'rows', 'attributes', 'values', 'struct', 'number', 'conversion', 'equivalent']);
  function inspect(value) {
    if (!value || typeof value !== 'object') return;
    for (const [key, child] of Object.entries(value)) {
      assert.equal(forbidden.has(key), false, `unexpected platform data key: ${key}`);
      inspect(child);
    }
  }
  inspect(catalog.presets);
  for (const item of catalog.presets) {
    assert.deepEqual(Object.keys(item).sort(), ['description', 'domain_hints', 'group', 'id', 'measure_type', 'measurements', 'name', 'size_systems']);
    for (const measurement of item.measurements) {
      assert.deepEqual(Object.keys(measurement).sort(), ['name', 'note', 'unit']);
      assert.ok(measurement.name && measurement.note);
      assert.ok(['cm', 'mm', ''].includes(measurement.unit));
    }
  }
  const topChest = catalog.getPreset('adult_tops').measurements.find(item => /胸宽/.test(item.name));
  assert.match(topChest.note, /单面宽度/);
  assert.match(topChest.note, /胸围/);
  const footwear = catalog.getPreset('adult_footwear').measurements;
  assert.match(footwear.find(item => /^足长/.test(item.name)).note, /不要填鞋垫或鞋内长度/);
  assert.match(footwear.find(item => /鞋内长/.test(item.name)).note, /不得从足长自动/);
  const innerDiameter = catalog.getPreset('rings').measurements.find(item => /内径/.test(item.name));
  assert.equal(innerDiameter.unit, 'mm');
  assert.match(innerDiameter.note, /内部圆孔的直径/);
});

test('retrieved nested data cannot be mutated by an editor or another shop', () => {
  const item = catalog.getPreset('adult_tops');
  const labels = catalog.getSizeSystem(item.id, 'alpha').labels;
  const first = labels[0];
  assert.ok(Object.isFrozen(catalog) && Object.isFrozen(catalog.presets) && Object.isFrozen(item));
  assert.ok(Object.isFrozen(item.measurements[0]) && Object.isFrozen(labels));
  assert.throws(() => { labels[0] = 'Changed'; }, TypeError);
  assert.throws(() => labels.push('Changed'), TypeError);
  assert.throws(() => { item.measurements[0].unit = 'in'; }, TypeError);
  assert.throws(() => { item.size_systems = []; }, TypeError);
  assert.equal(catalog.getSizeSystem(item.id, 'alpha').labels[0], first);
});

test('browser global and CommonJS expose the same serializable library', () => {
  const context = vm.createContext({});
  const source = fs.readFileSync(path.join(__dirname, '../bit/static/size-chart-presets.js'), 'utf8');
  vm.runInContext(source, context);
  assert.equal(context.SizeChartPresets.version, catalog.version);
  assert.equal(JSON.stringify(context.SizeChartPresets.presets), JSON.stringify(catalog.presets));
  assert.equal(context.SizeChartPresets.getPreset('adult_tops').id, 'adult_tops');
  assert.equal(context.SizeChartPresets.getSizeSystem('rings', 'us').labels[0], '3');
  assert.ok(Object.isFrozen(context.SizeChartPresets.getPreset('rings')));
});
