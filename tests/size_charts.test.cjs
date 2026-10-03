const {test} = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../bit/static/size-charts.js');

test('real measurement values retain official units without generated shoe conversions', () => {
  const field={id:'FOOT_LENGTH',name:'实际足长',value_type:'number_unit',allowed_units:[{id:'cm',name:'cm'}]};
  assert.deepEqual(ui.fieldValues(field,'24.5','cm'),[{name:'24.5 cm',struct:{number:24.5,unit:'cm'}}]);
  assert.deepEqual(ui.fieldValues(field,'','cm'),[]);
  assert.throws(() => ui.fieldValues(field,'24.5','in'),/官方允许/);
  assert.throws(() => ui.fieldValues(field,'Infinity','cm'),/有效/);
  assert.deepEqual(ui.fieldValues({id:'COUNT',value_type:'number'},'8'),[{name:'8'}]);
  assert.deepEqual(ui.readValue({values:[{name:'24.5 cm',struct:{number:24.5,unit:'cm'}}]}),{text:'24.5',unit:'cm',id:''});
  assert.deepEqual(ui.fieldValues({id:'GENDER',value_type:'list',values:[{id:'f',name:'女'}]},'f'),[{id:'f',name:'女'}]);
  assert.deepEqual(ui.fieldValues({id:'GENDER',value_type:'string',tags:['fixed'],values:[{id:'f',name:'女'}]},'f'),[{id:'f',name:'女'}]);
  assert.deepEqual(ui.fieldValues({id:'BRAND',value_type:'string',values:[{id:'brand1',name:'A'}]},'My Brand'),[{name:'My Brand'}]);
});
test('standard rows use official size IDs, leave measurements blank, and preserve existing measurements', () => {
  const field={id:'SIZE',value_type:'list',values:[{id:'small',name:'S'},{id:'medium',name:'M'}]};
  const existing=[{attributes:[{id:'SIZE',values:[{id:'small',name:'S'}]},{id:'CHEST',values:[{name:'48 cm',struct:{number:48,unit:'cm'}}]}]}];
  const result=ui.standardRows(field,['S','M','M'],existing,2);
  assert.deepEqual(result[0],existing[0]); assert.notEqual(result[0],existing[0]);
  assert.deepEqual(result[1],{attributes:[{id:'SIZE',values:[{id:'medium',name:'M'}]}]});
  assert.equal(ui.standardRows(field,['S'],[{attributes:[{id:'SIZE',values:[{name:'S'}]}]}],2).length,1);
  assert.throws(() => ui.standardRows(field,['S','XL'],existing,10),/官方可选尺码/);
  assert.deepEqual(existing[0].attributes[1].values[0].struct,{number:48,unit:'cm'});
  assert.throws(() => ui.standardRows(field,['S','M'],existing,1),/最多支持 1/);
  assert.deepEqual(ui.sizeLabels('S\nM，L;XL；2XL'),['S','M','L','XL','2XL']);
});
test('shoe main sizes support official numerical units without filling foot measurements or converting sizes', () => {
  const field={id:'M_US_SIZE',value_type:'number_unit',allowed_units:['US'],tags:['main_attribute_candidate']};
  assert.deepEqual(ui.standardRows(field,['5','5.5 US'],[],75,'US'),[
    {attributes:[{id:'M_US_SIZE',values:[{name:'5 US',struct:{number:5,unit:'US'}}]}]},
    {attributes:[{id:'M_US_SIZE',values:[{name:'5.5 US',struct:{number:5.5,unit:'US'}}]}]}
  ]);
  assert.throws(() => ui.standardRows(field,['5'],[],75,'EU'),/官方单位/);
  assert.throws(() => ui.standardRows(field,['35'],[],75,'US','eu'),/当前模板使用 EU/);
  const existing=[{attributes:[{id:'M_US_SIZE',values:[{id:'resolved-size-5',name:'5 US',struct:{number:5,unit:'US'}}]}]}];
  assert.equal(ui.standardRows(field,['5','5.0'],existing,75,'US').length,1);
  assert.throws(() => ui.standardRows({...field,tags:['body_measure']},['5'],[],75,'US'),/实际测量/);
});
test('full catalog download is a reference pack and cannot replace an individual chart draft', () => {
  const pack=ui.standardLibraryContent(); assert.equal(pack.presets.length,24);
  assert.equal(pack.format,'mercado-size-chart-standard-library');
  assert.throws(() => ui.templateContent(pack),/全品类参考模板包/);
});
test('imports preserve measure type, multi-values and official main structure, while discarding remote IDs', () => {
  const raw={id:99,revision:43,name:'模板',domain_id:'SHIRTS',measure_type:'MIXED_MEASURE',attributes:[{id:'BRAND',values:[{name:'Custom'}]}],main_attribute:{attributes:[{site_id:'CBT',id:'SIZE'}]},rows:[{id:'remote-row',attributes:[{id:'SIZE',values:[{name:'S'},{name:'M'}]}]}],names:{CBT:'Size Chart 1'}};
  const result=ui.templateContent(raw);
  assert.equal(result.id,undefined); assert.equal(result.revision,undefined); assert.equal(result.measure_type,'MIXED_MEASURE'); assert.equal(result.rows[0].attributes[0].values.length,2);
  assert.deepEqual(result.main_attribute,[{site_id:'CBT',id:'SIZE'}]);
  assert.throws(() => ui.templateContent({rows:{}}),/数组/);
  assert.throws(() => ui.templateContent({measure_type:'MIXED'}),/MIXED_MEASURE/);
  assert.equal(ui.targetFingerprint([{token_id:2,site_id:'MLB'},{token_id:1,site_id:'MLM'}]),ui.targetFingerprint([{token_id:1,site_id:'MLM'},{token_id:2,site_id:'MLB'}]));
  assert.equal(ui.rowLabel({payload:{main_attribute:{attributes:[{id:'SIZE'}]}}},{attributes:[{id:'SIZE',values:[{name:'XL'}]}]},0),'XL');
});

class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.dataset={}; this.attributes={}; this.listeners={}; this.value=''; this.disabled=false; this.hidden=false; this._text=''; }
  set innerHTML(_) { throw new Error('Unsafe innerHTML used'); }
  get textContent() { return this._text+this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text=String(value); this.children=[]; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text=''; this.children=[...children]; }
  addEventListener(event,callback) { this.listeners[event]=callback; }
  setAttribute(name,value) { this.attributes[name]=value; }
  setCustomValidity(text) { this.validityError=text; }
  checkValidity() { return !this.validityError; }
  reportValidity() { return this.checkValidity(); }
  showModal() { this.open=true; }
  close() { this.open=false; }
  focus() {}
}
const tick=() => new Promise(setImmediate);
const response=data => ({ok:true,status:200,json:async () => ({status:'success',data})});
const template=() => ({id:4,name:'女装上衣',domain_id:'SHIRTS',measure_type:'CLOTHING_MEASURE',attributes:[{id:'GENDER',values:[{id:'f',name:'女'}]}],main_attribute:[{site_id:'CBT',id:'SIZE'}],rows:[{attributes:[{id:'SIZE',values:[{name:'M'}]},{id:'CHEST',values:[{name:'48 cm',struct:{number:48,unit:'cm'}}]}]}],names:{CBT:'Shirts Size',MLM:'Shirts Size'},revision:2});
const schema=() => ({domain_id:'SHIRTS',domains:[{id:'SHIRTS',name:'上衣'}],identity:{site_id:'CBT',seller_id:42},max_rows:5,template_fields:[{id:'GENDER',name:'适用人群',value_type:'list',required:true,values:[{id:'f',name:'女'}]},{id:'BRAND',name:'品牌',value_type:'string',values:[{id:'b1',name:'Suggestion'}]}],row_fields:[{id:'SIZE',name:'尺码',value_type:'string',required:true},{id:'CHEST',name:'胸宽',value_type:'number_unit',allowed_units:['cm'],required:true,tags:['clothing_measure']}],main_attributes:[{id:'SIZE',name:'尺码'}],errors:[]});
function harness(custom,canManage=true) {
  const elements=new Map(), created=[];
  const get=id => { if (!elements.has(id)) { const el=new Element(); elements.set(id,el); created.push(el); } return elements.get(id); };
  const panel=get('size-charts'); panel.dataset.canManage=String(canManage);
  panel.querySelectorAll=selector => { const key=selector==='[data-draft-control]' ? 'draftControl' : 'targetControl'; return created.filter(el => el.dataset[key]); };
  const document={body:new Element(),getElementById:get,createElement:tag => { const el=new Element(tag); created.push(el); return el; }};
  const requests=[];
  const window={fetch:async (url,options) => { requests.push({url,options}); if (custom) { const data=await custom(url,options); if (data) return data; } if (url.endsWith('/templates')) return response({templates:[template()]}); if (url.endsWith('/options')) return response({stores:[{id:1,name:'店铺 A',site_id:'CBT',enabled:true,sites:[{site_id:'MLM'},{site_id:'MLB'}]}]}); if (url.endsWith('/records')) return response({records:[]}); if (url.endsWith('/requirements')) return response(schema()); return response({}); },setTimeout:() => 1,clearTimeout(){},addEventListener(){}};
  const app=ui.boot(document,window);
  return {app,get,created,requests,window};
}
const descendants=element => [element,...element.children.flatMap(descendants)];

test('read-only accounts cannot create, save, preview, upload or change rows',async () => {
  const h=harness(null,false); await tick(); h.app.selectTemplate(template()); await h.app.requirements();
  const count=h.requests.length; h.app.newTemplate(); await h.app.saveTemplate(); await h.app.preview(); await h.app.performUpload(); h.app.addRow();
  assert.equal(h.requests.length,count); assert.equal(h.app.state.draft.rows.length,1); assert.equal(h.get('sc-save').disabled,true); assert.equal(h.get('sc-upload').disabled,true);
  assert.equal(h.created.filter(el => el.dataset.draftControl).every(el => el.disabled),true);
});
test('official metadata and failed reads preserve existing real measurements and custom brand input',async () => {
  let fail=false;
  const h=harness(async url => { if (url.endsWith('/requirements') && fail) throw new Error('官方接口不可用'); }); await tick(); h.app.selectTemplate(template()); await h.app.requirements();
  assert.equal(h.app.state.draft.rows[0].attributes[1].values[0].struct.number,48);
  const brand=h.created.find(el => el.attributes['aria-label']==='品牌'); assert.equal(brand.tagName,'input'); brand.value='My Brand'; brand.listeners.input();
  assert.deepEqual(h.app.state.draft.attributes.find(item => item.id==='BRAND').values,[{name:'My Brand'}]);
  const previousSchema=h.app.state.schema; fail=true; await h.app.requirements(); assert.equal(h.app.state.schema,previousSchema); assert.match(h.get('sc-error').textContent,/官方接口不可用/); assert.equal(h.get('sc-requirements').disabled,false);
  assert.equal(h.app.state.draft.rows[0].attributes[1].values[0].struct.number,48);
});
test('saved preview invalidates on edits and target changes, and includes explicit CBT names',async () => {
  const h=harness(async url => { if (url.endsWith('/preview')) return response({preview_id:'p1',template_id:4,revision:2,expires_at:'2099-01-01T00:00:00Z',results:[{token_id:1,sites:['MLM'],status:'ready',message:'ok'}]}); }); await tick(); h.app.selectTemplate(template());
  const target=descendants(h.get('sc-targets')).find(el => el.tagName==='input'); target.checked=true; target.listeners.change();
  assert.match(h.get('sc-site-names').textContent,/跨境 · CBT/); await h.app.preview(); assert.equal(h.get('sc-upload').disabled,false); assert.equal(h.app.previewValid(),true);
  h.get('sc-name').value='修改名称'; h.get('sc-name').listeners.input(); assert.equal(h.app.state.preview,null); assert.equal(h.get('sc-upload').disabled,true); assert.equal(h.app.state.dirty,true);
  h.app.selectTemplate(template(),true); const target2=descendants(h.get('sc-targets')).find(el => el.tagName==='input'); target2.checked=true; target2.listeners.change(); await h.app.preview(); target2.checked=false; target2.listeners.change(); assert.equal(h.app.state.preview,null);
});
test('save sends optimistic revision and measure type; failures leave draft editable',async () => {
  let fail=false, saved;
  const h=harness(async (url,options) => { if (options.method==='PATCH') { saved=JSON.parse(options.body); if (fail) throw new Error('版本冲突'); return response({template:{...saved,id:4,revision:3}}); } }); await tick(); h.app.selectTemplate(template()); h.get('sc-name').value='保存名称'; h.get('sc-name').listeners.input(); await h.app.saveTemplate();
  assert.equal(saved.revision,2); assert.equal(saved.measure_type,'CLOTHING_MEASURE'); assert.equal(h.app.state.draft.revision,3); assert.equal(h.app.state.dirty,false);
  fail=true; h.get('sc-name').value='失败修改'; h.get('sc-name').listeners.input(); await h.app.saveTemplate(); assert.equal(h.app.state.dirty,true); assert.equal(h.get('sc-save').disabled,false); assert.match(h.get('sc-error').textContent,/版本冲突/);
});
test('import cannot replace current template identity and does not upload',async () => {
  const h=harness(); await tick(); h.app.selectTemplate(template()); h.get('sc-json').value=JSON.stringify({...template(),id:999,revision:99,name:'导入模板',measure_type:'MIXED_MEASURE'}); h.app.applyJSON();
  assert.equal(h.app.state.draft.id,4); assert.equal(h.app.state.draft.revision,2); assert.equal(h.app.state.draft.measure_type,'MIXED_MEASURE'); assert.equal(h.app.state.dirty,true); assert.equal(h.requests.some(request => request.url.endsWith('/upload')),false);
});
test('standard templates create reusable drafts and generate official rows without overwriting existing data',async () => {
  const h=harness(); await tick(); h.app.useStandard(); await tick();
  const draft=h.app.state.draft;
  assert.ok(draft.standard_template.preset_id); assert.equal(draft.domain_id,''); assert.deepEqual(draft.rows,[]);
  assert.equal(draft.id,undefined); assert.equal(h.app.state.dirty,true);
  h.get('sc-domain').value='SHIRTS'; h.get('sc-domain').listeners.input(); await h.app.requirements();
  const main=descendants(h.get('sc-main-attributes')).find(el => el.tagName==='select'); main.value='SIZE'; main.listeners.change();
  h.get('sc-standard-labels').value='S\nM'; h.app.generateStandardRows();
  assert.deepEqual(h.app.state.draft.rows.map(row => row.attributes[0].values[0].name),['S','M']);
  assert.equal(h.app.state.draft.rows.every(row => row.attributes.length===1),true);
  h.app.state.draft.rows[0].attributes.push({id:'CHEST',values:[{name:'48 cm',struct:{number:48,unit:'cm'}}]});
  h.app.generateStandardRows(); assert.equal(h.app.state.draft.rows.length,2); assert.equal(h.app.state.draft.rows[0].attributes[1].values[0].struct.number,48);
  h.get('sc-standard-labels').value='S\nM\nL\nXL\n2XL\n3XL'; h.app.generateStandardRows(); assert.equal(h.app.state.draft.rows.length,2); assert.match(h.get('sc-error').textContent,/最多支持 5/);
  const content=ui.templateContent(h.app.state.draft); assert.deepEqual(content.standard_template,draft.standard_template);
  assert.equal(h.requests.some(request => request.url.endsWith('/upload')),false);
});
test('read-only users can inspect standard templates but cannot use them or generate sizes',async () => {
  const h=harness(null,false); await tick(); const count=h.requests.length; h.app.useStandard(); h.app.generateStandardRows();
  assert.equal(h.app.state.draft,null); assert.equal(h.requests.length,count); assert.equal(h.get('sc-standard-use').disabled,true);
});
test('changing template filters or source store blocks stale schema generation while preserving measured rows',async () => {
  let fail=false;
  const h=harness(async url => { if (url.endsWith('/requirements') && fail) throw new Error('官方接口不可用'); }); await tick(); h.app.useStandard(); await tick();
  h.get('sc-domain').value='SHIRTS'; h.get('sc-domain').listeners.input(); await h.app.requirements();
  const main=descendants(h.get('sc-main-attributes')).find(el => el.tagName==='select'); main.value='SIZE'; main.listeners.change();
  h.get('sc-standard-labels').value='S'; h.app.generateStandardRows();
  h.app.state.draft.rows[0].attributes.push({id:'CHEST',values:[{name:'48 cm',struct:{number:48,unit:'cm'}}]});
  const gender=descendants(h.get('sc-template-fields')).find(el => el.tagName==='select'); gender.value='f'; gender.listeners.change();
  assert.equal(h.app.state.schemaStale,true); assert.equal(h.get('sc-standard-generate').disabled,true); assert.equal(h.get('sc-standard-official').disabled,true); assert.equal(h.get('sc-add-row').disabled,true);
  h.get('sc-standard-labels').value='M'; h.app.generateStandardRows(); h.app.addRow(); assert.equal(h.app.state.draft.rows.length,1);
  assert.equal(h.created.filter(el => el.dataset.requiresFresh).every(el => el.disabled),true);
  await h.app.requirements(); assert.equal(h.app.state.schemaStale,false); h.app.generateStandardRows(); assert.equal(h.app.state.draft.rows.length,2);
  h.get('sc-source-store').listeners.change(); await tick(); assert.equal(h.app.state.schemaStale,true); assert.equal(h.get('sc-standard-generate').disabled,true);
  await h.app.requirements(); const previous=h.app.state.schema; fail=true; await h.app.requirements();
  assert.equal(h.app.state.schema,previous); assert.equal(h.app.state.schemaStale,false); assert.equal(h.get('sc-standard-generate').disabled,false);
  assert.equal(h.app.state.draft.rows[0].attributes[1].values[0].struct.number,48);
});
test('upload requires reviewed preview, sends only preview_id, and exposes returned row IDs safely',async () => {
  const record={record_id:'r1',template_id:4,revision:2,template_snapshot:{name:'<img src=x onerror=bad()>'},token_id:1,sites:['MLM'],status:'succeeded',chart_id:'chart-42',payload:{main_attribute:{attributes:[{id:'SIZE'}]}},rows:[{id:'row-2',attributes:[{id:'SIZE',values:[{name:'M'}]}]}]}; let uploaded=false;
  const h=harness(async (url,options) => { if (url.endsWith('/preview')) return response({preview_id:'p2',template_id:4,revision:2,expires_at:'2099-01-01T00:00:00Z',results:[{token_id:1,sites:['MLM'],status:'ready'}]}); if (url.endsWith('/upload')) { uploaded=true; assert.deepEqual(JSON.parse(options.body),{preview_id:'p2'}); return response({results:[record],succeeded:1,failed:0,unknown:0}); } if (url.endsWith('/records') && uploaded) return response({records:[record]}); }); await tick(); h.app.selectTemplate(template()); h.app.state.targets=[{token_id:1,site_id:'MLM'}]; await h.app.performUpload(); assert.equal(uploaded,false); await h.app.preview(); await h.app.performUpload();
  assert.equal(uploaded,true); assert.equal(h.app.state.preview,null); assert.match(h.get('sc-records').textContent,/row-2/); assert.match(h.get('sc-records').textContent,/<img src=x onerror=bad\(\)>/); assert.equal(h.get('sc-upload').disabled,true);
});


test('saved measurements can be reviewed and uploaded to another site without reopening the editor',async () => {
  const h=harness(async url => url.endsWith('/preview') ? response({preview_id:'reuse',results:[{token_id:1,sites:['MLB'],status:'ready'}]}) : null);
  await tick(); const saved=template(); saved.rows[0].attributes[1].values.push({name:'50 cm',struct:{number:50,unit:'cm'}});
  h.app.selectTemplate(saved);
  assert.equal(h.get('sc-template-settings').open,false);
  assert.equal(h.get('sc-grid-wrap').hidden,false);
  assert.match(h.get('sc-grid-rows').textContent,/48 cm \/ 50 cm/);
  assert.equal(h.get('sc-grid-rows').textContent.includes('M'),true);
  const target=descendants(h.get('sc-targets')).filter(el => el.tagName==='input')[1];
  target.checked=true; target.listeners.change();
  const names=descendants(h.get('sc-site-names')).filter(el => el.tagName==='input');
  assert.equal(names[1].value,saved.name);
  assert.equal(h.app.state.dirty,false);
  await h.app.preview();
  assert.equal(h.get('sc-upload').disabled,false);
  assert.equal(h.requests.some(request => request.url.endsWith('/requirements')),false);
  assert.deepEqual(h.app.state.draft.rows,saved.rows);
});

test('using a built-in saves all values without requiring an official schema or manual input',async () => {
  const library=require('../bit/static/size-chart-builtins.js');
  let content;
  const h=harness(async (url,options) => {
    if (url.endsWith('/templates') && options.method==='POST') {
      content=JSON.parse(options.body); return response({template:{...content,id:50,revision:1}});
    }
  });
  await tick();
  assert.match(h.get('sc-builtin-rows').textContent,/82–88/);
  await h.app.useBuiltin();
  assert.deepEqual(content,library.templates[0].template);
  assert.equal(h.app.state.draft.id,50);
  assert.equal(h.app.state.dirty,false);
  assert.equal(h.get('sc-template-settings').open,false);
  assert.match(h.get('sc-grid-head').textContent,/胸围下限/);
  assert.equal(h.get('sc-reference-note').hidden,false);
  assert.equal(h.requests.some(request => request.url.endsWith('/requirements') || request.url.endsWith('/upload')),false);
});

test('built-in creation failure keeps filled draft, and read-only users only inspect references',async () => {
  const failed=harness(async (url,options) => { if (options.method==='POST') throw new Error('保存服务不可用'); });
  await tick(); await failed.app.useBuiltin();
  assert.equal(failed.app.state.dirty,true); assert.equal(failed.app.state.draft.rows.length,7);
  assert.equal(failed.app.state.draft.id,undefined); assert.match(failed.get('sc-error').textContent,/保存服务不可用/);
  const read=harness(null,false); await tick(); const count=read.requests.length;
  await read.app.useBuiltin(); assert.equal(read.requests.length,count); assert.equal(read.app.state.draft,null);
  assert.equal(read.get('sc-builtin-use').disabled,true); assert.equal(read.get('sc-builtin-rows').children.length,7);
});
