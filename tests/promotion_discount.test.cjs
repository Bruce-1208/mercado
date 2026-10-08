const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('bit/templates/mercado_promotions.html', 'utf8');
const code = html.slice(html.indexOf('function minimumDiscount'), html.indexOf('function renderItems'));
function setup(items, value='20') {
  const fields = {batchDiscountValue:{value}};
  items.forEach(row=>fields[`price-${row.id}`]={value:'old'});
  const context = {state:{items,selectedItems:new Set(items.map(r=>r.id)),preview:{}}, $:id=>fields[id],feedback:()=>{}};
  vm.createContext(context); vm.runInContext(code,context);
  return {context,fields};
}
test('minimum discount uses official maximum price and preserves unknown',()=>{
  const {context:c}=setup([]);
  assert.equal(c.discountLabel({original_price:100,max_price:80}),'至少降价 20%');
  assert.equal(c.minimumDiscount({original_price:100,max_price:null}),null);
  assert.equal(c.minimumDiscount({original_price:0,max_price:80}),null);
});
test('bulk discount updates eligible selected rows and skips out-of-range prices',()=>{
  const {context:c,fields}=setup([
    {id:1,original_price:100,min_price:70,max_price:80,status_raw:'candidate'},
    {id:2,original_price:100,max_price:75,status_raw:'candidate'},
    {id:3,original_price:100,status_raw:'started'},
    {id:4,original_price:null,status_raw:'candidate'},
    {id:5,original_price:100,status_raw:'candidate'},
  ]);
  c.state.selectedItems.delete(5);c.applyBatchDiscount();
  assert.equal(fields['price-1'].value,'80.00');
  for(const id of [2,3,4,5])assert.equal(fields[`price-${id}`].value,'old');
  assert.equal(c.state.preview,null);
});
test('invalid discounts leave prices unchanged',()=>{
  for(const value of ['', '-1','100','NaN','Infinity']){
    const {context:c,fields}=setup([{id:1,original_price:100,status_raw:'candidate'}],value);
    c.applyBatchDiscount();assert.equal(fields['price-1'].value,'old');
  }
});
