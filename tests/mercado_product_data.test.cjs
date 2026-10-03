const test = require('node:test');
const assert = require('node:assert/strict');
const data = require('../browser_extension/zeshun_collector/mercado-data.js');
test('listing IDs prefer selected seller and reject catalog IDs', () => {
  assert.equal(data.listingId('https://www.mercadolibre.com.mx/p/MLM12345?wid=MLM6226808220'), 'MLM6226808220');
  assert.equal(data.listingId('https://www.mercadolibre.com.mx/p/MLM12345'), '');
  assert.equal(data.listingId('https://articulo.mercadolibre.com.mx/MLM-6226808220-test'), 'MLM6226808220');
});
test('server measurements allow collection without a third party DOM snapshot', () => {
  const product = {source_item_id:'MLM6226808220',main_image_url:'https://example.test/a.jpg',source:{},plugin_snapshot:{}};
  const info = {sku:'MLM6226808220',weight_g:1300,size_cm:[25,33,3],volumetric_weight_g:413,sales:0,
    fulfillment_type:'self_ship',fulfillment_label:'自发货',fulfillment_eligible:true,seller_country:'CN'};
  data.apply(product, {product:info,fetched_at:100});
  assert.equal(product.scrape_status,'ok');
  assert.equal(product.source.sold_quantity,0);
  assert.equal(product.weight_g,1300);
  assert.equal(product.volumetric_weight_kg,.413);
  assert.equal(product.plugin_snapshot.read_method,'zeshun_server_api');
  assert.equal(product.plugin_snapshot.dom_lines,undefined);
  assert.throws(()=>data.apply(product,{product:{...info,sku:'MLM999999'}}));
  data.apply(product,{product:{...info,size_cm:null}});
  assert.equal(product.scrape_status,'partial');
});
test('card shows zero sales and unavailable fields distinctly', () => {
  const rows = data.rows({sku:'MLM6226808220',sales:0,commission_percent:0,listed_at:'2026-09-15T04:29:13',fulfillment_label:'自发货'}, Date.parse('2026-10-01T00:00:00Z'));
  assert.equal(rows.find(r=>r[0]==='销量 / 佣金')[1],'0 / 0%');
  assert.equal(rows.find(r=>r[0]==='上架')[1],'2026-09-15（16天前）');
  assert.equal(rows.find(r=>r[0]==='预计净收益')[1],'— / —');
});
