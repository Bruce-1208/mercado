'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const library=require('../bit/static/size-chart-builtins.js');
const ui=require('../bit/static/size-charts.js');

test('all built-in charts contain complete, finite ranges and portable platform content',() => {
  assert.equal(library.templates.length,21);
  assert.equal(new Set(library.templates.map(item => item.id)).size,21);
  for (const item of library.templates) {
    const chart=item.template;
    assert.equal(library.get(item.id),item);
    assert.equal(chart.measure_type,'BODY_MEASURE');
    assert.ok(chart.domain_id && chart.rows.length>=6);
    assert.equal(chart.attributes.find(attr => attr.id==='BRAND').values[0].name,'Generic');
    assert.ok(chart.attributes.find(attr => attr.id==='GENDER').values[0].id);
    assert.ok(Object.values(chart.names).every(ui.officialNameValid));
    assert.deepEqual(ui.templateContent(JSON.parse(JSON.stringify(chart))),chart);
    const sizes=new Set();
    for (const row of chart.rows) {
      const attrs=new Map(row.attributes.map(attr => [attr.id,attr.values[0]]));
      assert.ok(!sizes.has(attrs.get('SIZE').name)); sizes.add(attrs.get('SIZE').name);
      for (const [id,value] of attrs) {
        assert.ok(value.name);
        if (id.endsWith('_FROM')) {
          const upper=attrs.get(id.replace('_FROM','_TO'));
          assert.ok(Number.isFinite(value.struct.number) && Number.isFinite(upper.struct.number));
          assert.ok(value.struct.number>0 && upper.struct.number>=value.struct.number);
          assert.equal(value.struct.unit,'cm'); assert.equal(upper.struct.unit,'cm');
        }
      }
      assert.ok(row.attributes.filter(attr => attr.values[0].struct).length>=6);
      assert.equal('id' in row,false);
    }
    for (const key of ['seller_id','token_id','id','revision','chart_id']) assert.equal(key in chart,false);
  }
});

test('reference profiles stay immutable across import and editing',() => {
  const item=library.templates[0], draft=ui.templateContent(item.template);
  const original=item.template.rows[0].attributes[2].values[0].struct.number;
  draft.rows[0].attributes[2].values[0].struct.number=999;
  assert.equal(library.get(item.id).template.rows[0].attributes[2].values[0].struct.number,original);
  assert.throws(() => item.template.rows.push({attributes:[]}),TypeError);
  assert.equal(library.get('__proto__'),null);
  assert.match(library.note,/参考数据/);
});
test('server batch catalog exactly matches the browser reference templates', () => {
  const fs=require('node:fs'), path=require('node:path');
  const catalog=JSON.parse(fs.readFileSync(path.join(__dirname,'../bit/size-chart-builtins.json'),'utf8'));
  const browser=require('../bit/static/size-chart-builtins.js');
  assert.equal(catalog.version,browser.version);
  assert.deepEqual(catalog.templates,browser.templates.map(item => item.template));
});
